import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from itertools import chain

import pynvim
from pynvim.api import Buffer
from molten.code_cell import CodeCell
from molten.images import Canvas, get_canvas_given_provider, WeztermCanvas
from molten.info_window import create_info_window
from molten.ipynb import export_outputs, get_default_import_export_file, import_outputs
from molten.save_load import MoltenIOError, get_default_save_file, load, save
from molten.moltenbuffer import MoltenKernel
from molten.options import MoltenOptions
from molten.outputbuffer import OutputBuffer
from molten.position import DynamicPosition, Position
from molten.runtime import get_available_kernels
from molten.utils import MoltenException, notify_error, notify_info, notify_warn, nvimui
from pynvim import Nvim

import time
import sys
import traceback

import threading
import queue

import multiprocessing
import signal

import io
import pickle

from types import ModuleType

import codeop

@pynvim.plugin
class Molten:
    """The plugin class. Provides an interface for interacting with the plugin via vim functions,
    user commands and user autocommands.

    Invariants that must be maintained in order for this plugin to work:
    - Any CodeCell which belongs to some MoltenKernel _a_ never overlaps with any CodeCell which
      belongs to some MoltenKernel _b_.
    """

    nvim: Nvim
    canvas: Optional[Canvas]
    initialized: bool

    highlight_namespace: int
    extmark_namespace: int

    timer: Optional[int]
    input_timer: Optional[int]

    options: MoltenOptions

    # list of nvim buf numbers to a list of MoltenKernels 'attached' to that buffer
    buffers: Dict[int, List[MoltenKernel]]
    # list of kernel names to the MoltenKernel object that handles that kernel
    # duplicate names are sufixed with (n)
    molten_kernels: Dict[str, MoltenKernel]

    def __init__(self, nvim: Nvim):
        self.nvim = nvim
        self.initialized = False

        self.canvas = None
        self.buffers = {}
        self.timer = None
        self.input_timer = None
        self.molten_kernels = {}

        self.eval_counter = 1
        self.eval_lock = threading.Lock()
        self.eval_queue = queue.Queue()
        self.eval_thread = threading.Thread(target=self._eval_worker, daemon=True)
        self.eval_thread.start()

        self.eval_gate = threading.Event()
        self.eval_gate.set()

        self.global_namespaces: Dict[int, Dict[str, Any]] = {}

        self.current_eval_process: Optional[multiprocessing.Process] = None
        self.current_eval_pid: Optional[int] = None
        self.current_eval_bufnr: Optional[int] = None
        self.eval_interrupted = False

    def _initialize(self) -> None:
        assert not self.initialized

        self.options = MoltenOptions(self.nvim)

        self.canvas = get_canvas_given_provider(self.nvim, self.options)
        self.canvas.init()

        self.highlight_namespace = self.nvim.funcs.nvim_create_namespace("molten-highlights")
        self.extmark_namespace = self.nvim.funcs.nvim_create_namespace("molten-extmarks")

        self.timer = self.nvim.eval(
            f"timer_start({self.options.tick_rate}, 'MoltenTick', {{'repeat': -1}})"
        )  # type: ignore

        self.input_timer = self.nvim.eval(
            f"timer_start({self.options.tick_rate}, 'MoltenTickInput', {{'repeat': -1}})"
        )  # type: ignore

        self._setup_highlights()
        self._set_autocommands()

        self.nvim.exec_lua("_prompt_init = require('prompt').prompt_init")
        self.nvim.exec_lua("_select_and_run = require('prompt').select_and_run")
        self.nvim.exec_lua("_prompt_init_and_run = require('prompt').prompt_init_and_run")

        self.initialized = True

    def _set_autocommands(self) -> None:
        self.nvim.command("augroup molten")
        self.nvim.command("autocmd CursorMoved  * call MoltenOnCursorMoved()")
        self.nvim.command("autocmd CursorMovedI * call MoltenOnCursorMoved()")
        self.nvim.command("autocmd WinScrolled  * call MoltenOnWinScrolled()")
        self.nvim.command("autocmd BufEnter     * call MoltenUpdateInterface()")
        self.nvim.command("autocmd BufLeave     * call MoltenBufLeave()")
        self.nvim.command("autocmd BufUnload    * call MoltenOnBufferUnload()")
        self.nvim.command("autocmd ExitPre      * call MoltenOnExitPre()")
        self.nvim.command("augroup END")

    def _setup_highlights(self) -> None:
        self.nvim.exec_lua("_hl_utils = require('hl_utils')")
        hl_utils = self.nvim.lua._hl_utils
        hl_utils.set_default_highlights(self.options.hl.defaults)

    def _deinitialize(self) -> None:
        for molten_kernels in self.buffers.values():
            for molten_kernel in molten_kernels:
                molten_kernel.deinit()
        if self.canvas is not None:
            self.canvas.deinit()
        if self.timer is not None:
            self.nvim.funcs.timer_stop(self.timer)
        if self.input_timer is not None:
            self.nvim.funcs.timer_stop(self.input_timer)

    def _initialize_if_necessary(self) -> None:
        if not self.initialized:
            self._initialize()

    def _get_current_buf_kernels(self, requires_instance: bool) -> Optional[List[MoltenKernel]]:
        self._initialize_if_necessary()

        maybe_molten = self.buffers.get(self.nvim.current.buffer.number)
        if requires_instance and (maybe_molten is None or len(maybe_molten) == 0):
            raise MoltenException(
                "Molten is not initialized in this buffer; run `:VolcanoInit` to initialize."
            )
        return maybe_molten

    def _clear_on_buf_leave(self) -> None:
        if not self.initialized:
            return

        for molten_kernels in self.buffers.values():
            for molten_kernel in molten_kernels:
                molten_kernel.clear_interface()
                molten_kernel.clear_open_output_windows()

    def _clear_interface(self) -> None:
        if not self.initialized:
            return

        for molten_kernels in self.buffers.values():
            for molten_kernel in molten_kernels:
                molten_kernel.clear_virt_outputs()
        self._clear_on_buf_leave()

    def _update_interface(self) -> None:
        """Called on load, show_output/hide_output and buf enter"""
        if not self.initialized:
            return

        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            return

        for m in molten_kernels:
            m.update_interface()

    def _on_cursor_moved(self, scrolled=False) -> None:
        if not self.initialized:
            return

        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            return

        for m in molten_kernels:
            m.on_cursor_moved(scrolled)

    def _initialize_buffer(self, kernel_name: str, shared=False) -> MoltenKernel | None:
        assert self.canvas is not None
        if shared:  # use an existing molten kernel, for a new neovim buffer
            molten = self.molten_kernels.get(kernel_name)
            if molten is not None:
                molten.add_nvim_buffer(self.nvim.current.buffer)
                self.buffers[self.nvim.current.buffer.number] = [molten]
                return molten

            notify_warn(
                self.nvim,
                f"No running kernel {kernel_name} to share. Continuing with a new kernel.",
            )

        kernel_id = kernel_name
        if self.molten_kernels.get(kernel_name) is not None:
            kernel_id = f"{kernel_name}_{len(self.molten_kernels)}"

        try:
            molten = MoltenKernel(
                self.nvim,
                self.canvas,
                self.highlight_namespace,
                self.extmark_namespace,
                self.nvim.current.buffer,
                self.options,
                kernel_name,
                kernel_id,
            )

            self.add_kernel(self.nvim.current.buffer, kernel_id, molten)
            molten._doautocmd("VolcanoInitPost")
            if isinstance(self.canvas, WeztermCanvas):
                self.canvas.wezterm_split()

            return molten
        except Exception as e:
            notify_error(
                self.nvim, f"Could not initialize kernel named '{kernel_name}'.\nCaused By: {e}"
            )

    def add_kernel(self, buffer: Buffer, kernel_id: str, kernel: MoltenKernel):
        """Add a new MoltenKernel to be tracked by Molten.
        - Adds the new kernel to the buffer list for the given buffer
        - Adds the new kernel to the molten_kernels list, with a suffix if the name is already taken
        """
        if self.buffers.get(buffer.number) is None:
            self.buffers[buffer.number] = [kernel]
        else:
            self.buffers[buffer.number].append(kernel)

        self.molten_kernels[kernel_id] = kernel

    def _move_cursor_to(self, win, line):
        win.cursor = (line + 1, 0)

    def _clean_output_blocks(self, lines: List[str]) -> List[str]:
        source = "\n".join(lines)
        open_tags = source.count("<output>")
        close_tags = source.count("</output>")
        if open_tags == close_tags and (open_tags + close_tags) % 2 == 0:
            cleaned = re.sub(r"^<output>.*?</output>\n?", "", source, flags=re.DOTALL | re.MULTILINE)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            cleaned = cleaned.rstrip()
            return cleaned.splitlines()
        elif open_tags != close_tags or (open_tags + close_tags) % 2 != 0:
            return False

    def _switch_cell_type(self, direction: str) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0]

        tag_order = ["<cell>", "<markdown>", "<raw>"]
        closing_tag_order = ["</cell>", "</markdown>", "</raw>"]

        def rotate_tag(tag, is_closing):
            tags = closing_tag_order if is_closing else tag_order
            idx = tags.index(tag) if tag in tags else -1
            if idx == -1:
                return tag
            if direction == "forward":
                return tags[(idx + 1) % len(tags)]
            else:
                return tags[(idx - 1) % len(tags)]

        def run():
            row = cursor_row - 1

            def is_opening_tag(line):
                return line.strip() in tag_order

            def is_closing_tag(line):
                return line.strip() in closing_tag_order

            # Case 1: Cursor is directly on an opening tag
            if is_opening_tag(buf[row]):
                try:
                    next_line = 1
                    while True:
                        if is_closing_tag(buf[row + next_line]):
                            buf[row] = rotate_tag(buf[row], is_closing=False)
                            buf[row + next_line] = rotate_tag(buf[row + next_line], is_closing=True)
                            return
                        next_line += 1
                except IndexError:
                    return

            # Case 2: Cursor is directly on a closing tag
            elif is_closing_tag(buf[row]):
                try:
                    prev_line = -1
                    while True:
                        if is_opening_tag(buf[row + prev_line]):
                            buf[row + prev_line] = rotate_tag(buf[row + prev_line], is_closing=False)
                            buf[row] = rotate_tag(buf[row], is_closing=True)
                            return
                        prev_line -= 1
                except IndexError:
                    return

            # Case 3: Cursor is inside a block (must find BOTH tags)
            else:
                try:
                    # Search upward for opening tag
                    start = row
                    while start >= 0 and not is_opening_tag(buf[start]):
                        if is_closing_tag(buf[start]):
                            return  # hit another block, not inside one
                        start -= 1

                    if start < 0 or not is_opening_tag(buf[start]):
                        return

                    # Search downward for closing tag
                    end = row
                    while end < len(buf) and not is_closing_tag(buf[end]):
                        if is_opening_tag(buf[end]):
                            return  # hit another block, not inside one
                        end += 1

                    if end >= len(buf) or not is_closing_tag(buf[end]):
                        return

                    # Valid block boundaries found
                    buf[start] = rotate_tag(buf[start], is_closing=False)
                    buf[end] = rotate_tag(buf[end], is_closing=True)
                except IndexError:
                    return

        self.nvim.async_call(run)

    def _create_cell(self, direction: str) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0]
        tag_order = ["<cell>", "<markdown>", "<raw>"]
        closing_tag_order = ["</cell>", "</markdown>", "</raw>"]

        def append_cell_block(row: int, direction: str, empty_line=False, middle_line=False):
            if direction == "upward":
                new_line = 0
                if empty_line == False:
                    buf.append("", row + new_line)
                buf.append("</cell>", row + new_line)
                buf.append("", row + new_line)
                buf.append("<cell>", row + new_line)
                if middle_line == False:
                    buf.append("", row + new_line)

            elif direction == "downward":
                new_line = 1
                if empty_line == False:
                    buf.append("", row + new_line)
                    new_line += 1
                buf.append("<cell>", row + new_line)
                new_line += 1
                buf.append("", row + new_line)
                new_line += 1
                buf.append("</cell>", row + new_line)
                new_line += 1
                if middle_line == False:
                    buf.append("", row + new_line)

        def create_cell(row: int, direction: str):
            opening_tag, closing_tag = "<cell>", "</cell>"
            if direction == "upward":
                if row == 0:
                    if buf[row] in tag_order:
                        append_cell_block(row, direction)
                        self.nvim.current.window.cursor = (row + 3, 0)
                    elif buf[row] == "":
                        append_cell_block(row, direction, empty_line=True)
                        self.nvim.current.window.cursor = (row + 3, 0)
                elif row > 0:
                    if buf[row] in tag_order:
                        append_cell_block(row, direction, middle_line=True)
                        self.nvim.current.window.cursor = (row + 2, 0)
                    elif buf[row] not in tag_order:
                        while row > 0:
                            row -= 1
                            if buf[row] in tag_order:
                                if row >= 2:
                                    append_cell_block(row, direction, middle_line=True)
                                    self.nvim.current.window.cursor = (row + 2, 0)
                                    break
                                elif buf[row - 1] == "":
                                    append_cell_block(row, direction, middle_line=True)
                                    self.nvim.current.window.cursor = (row + 2, 0)
                                    break
                                else:
                                    append_cell_block(row, direction)
                                    break
                            elif row == 0:
                                if buf[row] in tag_order:
                                    append_cell_block(row, direction)
                                    break
                                elif buf[row] == "":
                                    append_cell_block(row, direction, empty_line=True)
                                    self.nvim.current.window.cursor = (row + 2, 0)
                                    break

            elif direction == "downward":
                if row == len(buf) - 1:
                    if buf[row] in closing_tag_order:
                        append_cell_block(row, direction)
                    elif buf[row] == "":
                        append_cell_block(row, direction, empty_line=True)
                        self.nvim.current.window.cursor = (row + 3, 0)
                elif row < len(buf) - 1:
                    if buf[row] in closing_tag_order:
                        append_cell_block(row, direction, middle_line=True)
                        self.nvim.current.window.cursor = (row + 4, 0)
                    elif buf[row] not in closing_tag_order:
                        while row < len(buf) - 1:
                            row += 1
                            if buf[row] in closing_tag_order:
                                if row <= len(buf) - 2:
                                    append_cell_block(row, direction, middle_line=True)
                                    self.nvim.current.window.cursor = (row + 4, 0)
                                    break
                                elif buf[len(buf) - 1] == "":
                                    append_cell_block(row, direction, middle_line=True)
                                    self.nvim.current.window.cursor = (row + 4, 0)
                                else:
                                    append_cell_block(row, direction)
                                    break
                            elif row == len(buf) - 1:
                                if buf[row] in closing_tag_order:
                                    append_cell_block(row, direction)
                                elif buf[row] == "":
                                    append_cell_block(row, direction, empty_line=True)
                                    self.nvim.current.window.cursor = (row + 4, 0)
                                break

        def run():
            row = cursor_row - 1
            create_cell(row, direction)

        self.nvim.async_call(run)

    def _move_cell(self, direction: str) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0]
        total_lines = len(buf)

        def find_cell_boundaries(row: int):
            """Find <cell> and </cell> boundaries around cursor."""
            start, end = None, None

            # Find start (<cell>)
            for i in range(row, -1, -1):
                if buf[i].strip() == "<cell>":
                    start = i
                    break

            # Find end (</cell>)
            for j in range(row, total_lines):
                if buf[j].strip() == "</cell>":
                    end = j
                    break

            return start, end

        def find_adjacent_cell(direction: str, start: int, end: int):
            """Find the next cell boundaries above or below."""
            if direction == "upward":
                for i in range(start - 1, -1, -1):
                    if buf[i].strip() == "</cell>":
                        upper_end = i
                        # find matching <cell>
                        for j in range(i, -1, -1):
                            if buf[j].strip() == "<cell>":
                                return j, upper_end
                return None, None

            elif direction == "downward":
                for i in range(end + 1, total_lines):
                    if buf[i].strip() == "<cell>":
                        lower_start = i
                        # find matching </cell>
                        for j in range(i, total_lines):
                            if buf[j].strip() == "</cell>":
                                return lower_start, j
                return None, None

        def move_cell(direction: str, start: int, end: int):
            """Move the cell upward or downward."""
            cell_lines = buf[start:end + 1]

            if direction == "upward":
                adj_start, adj_end = find_adjacent_cell("upward", start, end)
                if adj_start is None:
                    self.nvim.out_write("No cell above.\n")
                    return
                above_block = buf[adj_start:adj_end + 1]
                buf[adj_start:end + 1] = cell_lines + [""] + above_block
                self.nvim.current.window.cursor = (adj_start + 1, 0)

            elif direction == "downward":
                adj_start, adj_end = find_adjacent_cell("downward", start, end)
                if adj_start is None:
                    self.nvim.out_write("No cell below.\n")
                    return
                below_block = buf[adj_start:adj_end + 1]
                buf[start:adj_end + 1] = below_block + [""] + cell_lines
                self.nvim.current.window.cursor = (adj_start + 1, 0)

        def run():
            row = cursor_row - 1
            start, end = find_cell_boundaries(row)
            if start is None or end is None:
                self.nvim.out_write("Cursor not inside a valid <cell> block.\n")
                return
            move_cell(direction, start, end)

        self.nvim.async_call(run)

    def _evaluate_cell(self, delay: bool = False):
        buf = self.nvim.current.buffer
        win = self.nvim.current.window
        cursor_pos = win.cursor
        cur_line = self.nvim.funcs.line('.') - 1
        total_lines = len(buf)

        # Find cell block containing cursor
        active_block = None
        for i in range(total_lines):
            if buf[i].strip() == "<cell>":
                start = i
                i += 1
                while i < total_lines and buf[i].strip() != "</cell>":
                    i += 1
                if i < total_lines and buf[i].strip() == "</cell>":
                    end = i
                    if start <= cur_line <= end:
                        active_block = (start, end)
                        break

        if not active_block:
            return

        start_line, end_line = active_block
        expr_lines = buf[start_line + 1:end_line]
        expr = "\n".join(expr_lines).strip()

        # Detect shell command syntax (e.g., "!pip install requests")
        if expr.startswith("!"):
            shell_cmd = expr[1:].strip()
            if not shell_cmd:
                return

            # --- Clear existing <output> block before inserting new one ---
            output_start = None
            output_end = None
            for i in range(end_line + 1, len(buf)):
                line = buf[i].strip()
                if line == "<cell>":
                    break
                if line == "<output>":
                    output_start = i
                elif line == "</output>":
                    output_end = i
                    break
            if output_start is not None and output_end is not None:
                if output_end + 1 < len(buf) and buf[output_end + 1].strip() == "":
                    output_end += 1
                buf.api.set_lines(output_start, output_end + 1, False, [])
                self.nvim.command("undojoin")

            # Insert placeholder output
            placeholder = ["", "<output>", f"[Shell][*] running: {shell_cmd}", "</output>", ""]
            buf.api.set_lines(end_line + 1, end_line + 1, False, placeholder)
            self.nvim.command("undojoin")

            def run_shell():
                import subprocess, sys
                try:
                    # Use same Python env for pip commands
                    if shell_cmd.startswith("pip "):
                        cmd = [sys.executable, "-m"] + shell_cmd.split()
                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                    else:
                        proc = subprocess.Popen(
                            shell_cmd,
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                    output, _ = proc.communicate()
                except Exception as e:
                    output = f"Error executing shell command:\n{e}"

                # Safely update buffer on main thread
                def update_output():
                    lines = ["", "<output>"] + output.splitlines() + ["</output>"]
                    buf.api.set_lines(end_line + 1, end_line + 6, False, lines)

                self.nvim.async_call(update_output)

            threading.Thread(target=run_shell, daemon=True).start()
            return

        if not expr:
            return  # Empty cell, skip evaluation

        with self.eval_lock:
            eval_id = self.eval_counter
            self.eval_counter += 1

        # Skip evaluation if expr is empty or only whitespace
        if not expr.strip():
            return

        # Remove any blank lines after </cell>
        i = end_line + 1
        delete_to = i
        while delete_to < len(buf):
            line = buf[delete_to].strip()
            if line in ("<output>", "<cell>", "</output>", "</cell>") or line != "":
                break
            delete_to += 1
        if delete_to > i:
            buf.api.set_lines(i, delete_to, False, [])

        # Remove existing <output> block (only up to next <cell>)
        output_start = None
        output_end = None
        for i in range(end_line + 1, len(buf)):
            line = buf[i].strip()
            if line == "<cell>":
                break
            if line == "<output>":
                output_start = i
            elif line == "</output>":
                output_end = i
                break
        if output_start is not None and output_end is not None:
            if output_end + 1 < len(buf) and buf[output_end + 1].strip() == "":
                output_end += 1
            buf.api.set_lines(output_start, output_end + 1, False, [])
            self.nvim.command("undojoin")

        # Insert new output placeholder block
        placeholder = ["", "<output>", f"[{eval_id}][*] queue...", "</output>", ""]
        buf.api.set_lines(end_line + 1, end_line + 1, False, placeholder)
        self.nvim.command("undojoin")

        # Queue up async evaluation
        self.eval_queue.put({
            "bufnr": buf.number,
            "expr": expr,
            "start_line": start_line,
            "end_line": end_line,
            "eval_id": eval_id,
            "cursor_pos": cursor_pos,
            "win_handle": win.handle,
            "delay": delay, 
        })

    def _find_cell_regions(self, buf_lines: List[str]) -> List[Tuple[int, int]]:
        """
        Return [(start_line, end_line)] for every <cell>...</cell> in the buffer.
        Lines are 0-based and inclusive of both tags.
        """
        i = 0
        regions = []
        n = len(buf_lines)
        while i < n:
            if buf_lines[i].strip() == "<cell>":
                start = i
                i += 1
                while i < n and buf_lines[i].strip() != "</cell>":
                    i += 1
                if i < n and buf_lines[i].strip() == "</cell>":
                    regions.append((start, i))
            i += 1
        return regions


    def _remove_output_block_in_slice(self, buf: Buffer, from_line: int, to_line_exclusive: int) -> None:
        """
        Remove a single <output>...</output> block if present between [from_line, to_line_exclusive).
        Only removes up to the next <cell>.
        """
        output_start = None
        output_end = None
        for i in range(from_line, min(to_line_exclusive, len(buf))):
            s = buf[i].strip()
            if s == "<cell>":
                break
            if s == "<output>":
                output_start = i
            elif s == "</output>":
                output_end = i
                break
        if output_start is not None and output_end is not None:
            # Also trim a trailing blank line if present
            if output_end + 1 < len(buf) and buf[output_end + 1].strip() == "":
                output_end += 1
            buf.api.set_lines(output_start, output_end + 1, False, [])
            self.nvim.command("undojoin")

    def _evaluate_all_cells(self, up_to_cursor=None):
        pass

    def _eval_worker(self):
        """
        Background evaluation worker.
        - Pulls jobs from self.eval_queue continuously.
        - Waits on self.eval_gate before *executing* jobs.
        - Ensures queued jobs accumulate safely while gate is closed.
        """
        while True:
            try:
                # Always get next job (even if gate is closed, so queue can fill)
                item = self.eval_queue.get()

                # Shutdown signal
                if item is None:
                    self.eval_queue.task_done()
                    break

                # Wait until evaluations are allowed to proceed
                self.eval_gate.wait()

                try:
                    # Perform the actual evaluation (your existing logic)
                    self._evaluate_and_update(**item)
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    self.nvim.async_call(lambda: notify_error(f"[Molten eval_worker] {e}\n{tb}"))

                self.eval_queue.task_done()

            except Exception as outer_e:
                import traceback
                tb = traceback.format_exc()
                self.nvim.async_call(
                    lambda: notify_error(f"[Molten eval_worker outer loop] {outer_e}\n{tb}")
                )
                # continue looping even after unexpected errors
                continue

    def _evaluate_and_update(self, bufnr, expr, start_line, end_line, eval_id, cursor_pos, win_handle, delay=False):
        """Evaluate a <cell> block, stream its output, and persist its namespace per-chunk."""

        def notify_error_async(msg):
            self.nvim.async_call(lambda: self.nvim.command(
                f'echohl ErrorMsg | echom "[Eval Error] {msg}" | echohl None'))

        def update_output_block(lines):
            def _do_update():
                try:
                    buf = self.nvim.buffers[bufnr]
                    code_lines = expr.splitlines()
                    found = False
                    cell_start = None
                    cell_end = None
                    in_cell = False
                    code_index = 0
                    for i in range(len(buf)):
                        line = buf[i]
                        stripped = line.strip()
                        if stripped == "<cell>":
                            in_cell = True
                            cell_start = i
                            code_index = 0
                            continue
                        if in_cell:
                            if stripped == "</cell>":
                                in_cell = False
                                cell_end = i
                                if code_index == len(code_lines):
                                    found = True
                                    break
                                else:
                                    code_index = 0
                                    continue
                            if code_index < len(code_lines) and line.rstrip() == code_lines[code_index].rstrip():
                                code_index += 1
                            else:
                                in_cell = False
                                code_index = 0
                    if not found:
                        notify_error_async("Cell not found in buffer for evaluation")
                        return

                    end_line_current = cell_end
                    out_start, out_end = None, None
                    found_output = False
                    for j in range(end_line_current + 1, len(buf)):
                        line = buf[j].strip()
                        if not found_output and line == "<output>":
                            out_start = j
                            found_output = True
                        elif found_output and line == "</output>":
                            out_end = j
                            break
                    while lines and not lines[-1].strip():
                        lines.pop()
                    if out_start is not None and out_end is not None:
                        buf.api.set_lines(out_start + 1, out_end, False, lines)
                        self.nvim.command("undojoin")
                    else:
                        insert_lines = ["<output>"] + lines + ["</output>"]
                        buf.api.set_lines(end_line_current + 1, end_line_current + 1, False, insert_lines)
                        self.nvim.command("undojoin")
                except Exception as e:
                    notify_error_async(f"Stream update error: {e}")
            self.nvim.async_call(_do_update)

        # ---- Persistent global namespace setup ----
        ns_result = []
        ns_ready = threading.Event()

        def _compute_ns_path_on_main():
            try:
                fname = self.nvim.eval("expand('%:p')")
                if not fname:
                    fname = f"buffer_{bufnr}"
                checkpoint_dir = os.path.dirname(fname)
                os.makedirs(checkpoint_dir, exist_ok=True)
                ns_path = os.path.join(checkpoint_dir, f"{os.path.basename(fname)}.pkl")
                if os.path.exists(ns_path):
                    try:
                        with open(ns_path, "rb") as f:
                            ns = pickle.load(f)
                    except Exception:
                        ns = {"variables": {}, "import_lines": []}
                else:
                    ns = {"variables": {}, "import_lines": []}
                self.global_namespaces[bufnr] = ns
                ns_result.append((ns_path, ns))
            finally:
                ns_ready.set()

        self.nvim.async_call(_compute_ns_path_on_main)
        ns_ready.wait()
        ns_path, ns = ns_result[0]

        # ---- Prepare state ----
        self.current_eval_expr = expr
        self.current_eval_start_line = start_line
        self.current_eval_end_line = end_line
        self.current_eval_eval_id = eval_id

        lines_so_far = [f"[{eval_id}][*] 0.00 seconds..."]
        update_output_block(lines_so_far.copy())
        if delay:
            time.sleep(0.3)

        output_queue = multiprocessing.Queue()

        # ---- Worker process ----
        def run_eval(code, q, pre_vars, import_lines_initial, ns_path):

            class StreamingStdout(io.TextIOBase):
                def __init__(self, qq):
                    self.q = qq
                    self._buffer = ""
                def write(self, text):
                    if not text:
                        return
                    self._buffer += text
                    while "\n" in self._buffer:
                        line, self._buffer = self._buffer.split("\n", 1)
                        self.q.put(("line", line))
                def flush(self):
                    if self._buffer:
                        self.q.put(("line", self._buffer))
                        self._buffer = ""

            def picklable_vars(globs, baseline_keys):
                out = {}
                for k, v in globs.items():
                    if k in baseline_keys or k.startswith("__"):
                        continue
                    if isinstance(v, ModuleType):
                        continue
                    try:
                        pickle.dumps(v)
                    except Exception:
                        continue
                    out[k] = v
                return out

            def extract_imports_from_src(src):
                imps = []
                for _line in src.splitlines():
                    s = _line.strip()
                    if s.startswith("import ") or s.startswith("from "):
                        imps.append(s)
                return imps

            def atomic_pickle_write(path, payload):
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    pickle.dump(payload, f)
                os.replace(tmp, path)

            error_happened = False
            globs = {}
            globs.update(pre_vars)

            imports_live = list(import_lines_initial) if import_lines_initial else []
            for imp in imports_live:
                try:
                    exec(imp, globs)
                except Exception:
                    pass

            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = StreamingStdout(q)

            compiler = codeop.CommandCompiler()
            lines = code.splitlines()
            buf_accum = []
            baseline_keys = set(globs.keys())
            start_idx = 0

            try:
                for i, line in enumerate(lines):
                    buf_accum.append(line)
                    src = "\n".join(buf_accum)
                    codeobj = compiler(src, filename="<string>", symbol="exec")
                    if codeobj is None:
                        continue
                    try:
                        exec(codeobj, globs)
                    except BaseException as e:
                        error_happened = True
                        tb = traceback.extract_tb(e.__traceback__)
                        code_lines = code.splitlines()
                        user_lineno = None
                        for frame in tb:
                            if frame.filename == "<string>":
                                user_lineno = frame.lineno + start_idx
                                break
                        q.put(("line", "-" * 75))
                        q.put(("line", f"{type(e).__name__}{' ' * 33}Traceback (most recent call last)"))
                        if user_lineno is not None and 1 <= user_lineno <= len(code_lines):
                            q.put(("line", f"Cell In[{eval_id}], line {user_lineno}"))
                            q.put(("line", f"----> {user_lineno} {code_lines[user_lineno - 1].strip()}"))
                            q.put(("line", ""))
                        q.put(("line", f"{type(e).__name__}: {e}"))
                        break

                    new_imps = extract_imports_from_src(src)
                    for imp in new_imps:
                        if imp not in imports_live:
                            imports_live.append(imp)
                    new_vars = picklable_vars(globs, baseline_keys)
                    baseline_keys = set(globs.keys())
                    q.put(("globals", (new_vars, new_imps)))

                    try:
                        if os.path.exists(ns_path):
                            try:
                                with open(ns_path, "rb") as f:
                                    ns_disk = pickle.load(f)
                            except Exception:
                                ns_disk = {"variables": {}, "import_lines": []}
                        else:
                            ns_disk = {"variables": {}, "import_lines": []}
                        ns_disk["variables"].update(new_vars)
                        for imp in imports_live:
                            if imp not in ns_disk.get("import_lines", []):
                                ns_disk.setdefault("import_lines", []).append(imp)
                        atomic_pickle_write(ns_path, ns_disk)
                    except Exception as _e:
                        q.put(("line", f"[persist warning] {type(_e).__name__}: {_e}"))

                    buf_accum = []
                    start_idx = i + 1
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                q.put(("done", error_happened))

        process = multiprocessing.Process(
            target=run_eval,
            args=(expr, output_queue, ns["variables"], ns.get("import_lines", []), ns_path),
        )
        process.start()
        self.current_eval_process = process
        self.current_eval_pid = process.pid
        self.current_eval_bufnr = bufnr

        start_time = time.time()
        last_update_time = start_time
        update_interval = 0.3
        saw_done = False
        error_occurred = False

        try:
            while True:
                got_item = False
                try:
                    kind, payload = output_queue.get(timeout=0.05)
                    got_item = True
                except queue.Empty:
                    pass
                if got_item:
                    if kind == "line":
                        lines_so_far.append(str(payload))
                    elif kind == "globals":
                        new_vars, new_imports = payload
                        ns["variables"].update(new_vars)
                        for imp in new_imports:
                            if imp not in ns.setdefault("import_lines", []):
                                ns["import_lines"].append(imp)
                    elif kind == "done":
                        saw_done = True
                        error_occurred = bool(payload)

                elapsed = time.time() - start_time
                lines_so_far[0] = f"[{eval_id}][*] {elapsed:.2f} seconds..."
                now = time.time()
                if now - last_update_time > update_interval:
                    update_output_block(lines_so_far.copy())
                    last_update_time = now
                if not process.is_alive() and saw_done:
                    break
                if not process.is_alive() and not got_item:
                    break
        finally:
            if process.is_alive():
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.join(timeout=1)
            self.current_eval_process = None
            self.current_eval_pid = None
            self.current_eval_bufnr = None

        elapsed = max(0.0, time.time() - start_time)

        # ---- Determine status (with traceback restoration) ----
        if getattr(self, "eval_restarted", False):
            lines_so_far[0] = f"[{eval_id}][Kernel_Restarted] {elapsed:.2f} seconds..."
            code_lines = expr.splitlines()
            idx = len(code_lines)
            while idx > 0 and not code_lines[idx - 1].strip():
                idx -= 1
            if idx >= 1:
                user_lineno = idx
                code_line = code_lines[user_lineno - 1].strip()
            else:
                user_lineno = 1
                code_line = ""
            lines_so_far += [
                "-" * 75,
                "KeyboardInterrupt" + " " * 25 + "Traceback (most recent call last)",
                f"Cell In[{eval_id}], line {user_lineno}",
                f"----> {user_lineno} {code_line}",
                "",
                "KeyboardInterrupt",
            ]
            self.eval_restarted = False

        elif getattr(self, "eval_interrupted", False):
            lines_so_far[0] = f"[{eval_id}][Kernel_Interrupted] {elapsed:.2f} seconds..."
            code_lines = expr.splitlines()
            idx = len(code_lines)
            while idx > 0 and not code_lines[idx - 1].strip():
                idx -= 1
            if idx >= 1:
                user_lineno = idx
                code_line = code_lines[user_lineno - 1].strip()
            else:
                user_lineno = 1
                code_line = ""
            lines_so_far += [
                "-" * 75,
                "KeyboardInterrupt" + " " * 25 + "Traceback (most recent call last)",
                f"Cell In[{eval_id}], line {user_lineno}",
                f"----> {user_lineno} {code_line}",
                "",
                "KeyboardInterrupt",
            ]
            self.eval_interrupted = False

        elif error_occurred:
            lines_so_far[0] = f"[{eval_id}][Error] {elapsed:.2f} seconds..."
        else:
            lines_so_far[0] = f"[{eval_id}][Done] {elapsed:.2f} seconds..."

        update_output_block(lines_so_far.copy())

        # ---- Final persist ----
        try:
            tmp = ns_path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(ns, f)
            os.replace(tmp, ns_path)
        except Exception as e:
            self.nvim.async_call(lambda: notify_error_async(f"Failed to save namespace: {e}"))


    def _restart_kernel(self):
        """Restart the entire Molten kernel environment and reset eval state."""
        # terminate any running evaluation process
        if self.current_eval_process and self.current_eval_process.is_alive():
            pid = self.current_eval_pid
            try:
                os.kill(pid, signal.SIGKILL)
                self.current_eval_process.join(timeout=1)
            except ProcessLookupError:
                pass
            except Exception:
                pass
            # flag for _evaluate_and_update to mark Kernel_Restarted
            self.eval_restarted = True

        # reset process tracking and interrupt flags
        self.current_eval_process = None
        self.current_eval_pid = None
        self.current_eval_bufnr = None
        self.eval_interrupted = False

        # reset evaluation counter and clear queue
        with self.eval_lock:
            self.eval_counter = 1
            while not self.eval_queue.empty():
                try:
                    self.eval_queue.get_nowait()
                except queue.Empty:
                    break

        # reset persistent namespaces
        self.global_namespaces.clear()

        # restart all existing Molten kernels
        restarted = []
        for name, kernel in list(self.molten_kernels.items()):
            try:
                kernel.shutdown()
            except Exception:
                pass
            try:
                new_kernel = MoltenKernel(self.nvim, kernel.name, kernel.options)
                self.molten_kernels[name] = new_kernel
                restarted.append(name)
            except Exception:
                pass

        # update queued cells to reflect that they were restarted
        try:
            for buf in self.nvim.buffers:
                lines = list(buf[:])
                changed = False
                for i, line in enumerate(lines):
                    # replace queued status indicator with Kernel_Restarted
                    if "[*]" in line and "queue" in line:
                        idx = line.find("[*]")
                        prefix = line[:idx]
                        lines[i] = f"{prefix}[Kernel_Restarted]"
                        changed = True
                if changed:
                    buf[:] = lines
        except Exception:
            pass

        def _delete_ns_path_main():
            try:
                bufnr = self.nvim.current.buffer.number
                fname = self.nvim.eval("expand('%:p')")
                if not fname:
                    fname = f"buffer_{bufnr}"
                checkpoint_dir = os.path.dirname(fname)
                ns_path = os.path.join(checkpoint_dir, f"{os.path.basename(fname)}.pkl")

                if not os.path.exists(ns_path):
                    return

                # One-shot nuke: kill anything using it, then remove it
                os.system(f"fuser -k '{ns_path}' >/dev/null 2>&1 || true")
                os.system(f"rm -f '{ns_path}' >/dev/null 2>&1 || true")

            except Exception as e:
                pass

        self.nvim.async_call(_delete_ns_path_main)

    @pynvim.command("VolcanoInit", nargs="*", sync=True, complete="file") 
    @nvimui 
    def command_init(self, args: List[str]) -> None:
        if not hasattr(self, "_volcano_debug_shown"):
            self._volcano_debug_shown = True

            filename = self.nvim.current.buffer.name

            if filename.endswith(".ipynb"):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        nb_data = json.load(f)

                    # Setup checkpoint directory and file path
                    basename = os.path.basename(filename)
                    dirname = os.path.dirname(filename)
                    checkpoint_dir = os.path.join(dirname, ".ipynb_checkpoints")
                    os.makedirs(checkpoint_dir, exist_ok=True)

                    interpreted_path = os.path.join(checkpoint_dir, f"{basename}_interpreted")

                    # Write as interpreted Python script
                    with open(interpreted_path, "w", encoding="utf-8") as f_out:
                        for cell in nb_data.get("cells", []):
                            if cell.get("cell_type") == "code":
                                f_out.write("<cell>\n")
                                source_lines = cell.get("source", [])
                                if source_lines:
                                    for line in source_lines:
                                        f_out.write(line if line.endswith("\n") else line + "\n")
                                else:
                                    f_out.write("\n")  # empty line for empty cells
                                f_out.write("</cell>\n\n")

                    # Verify the interpreted file exists
                    if os.path.isfile(interpreted_path):
                        # Switch to the interpreted file without creating a swap file
                        self.nvim.command(f"noswapfile edit {interpreted_path}")
                        self.nvim.command("setlocal noswapfile")
                        self.nvim.command("set filetype=ipynb_interpreted")

                    else:
                        self.nvim.command(f"echoerr 'Interpreted file {interpreted_path} was not created'")

                except Exception as e:
                    self.nvim.command(f"echoerr 'Failed to convert notebook: {e}'")

        self._initialize_if_necessary()

        shared = False
        if len(args) > 0 and args[0] == "shared":
            shared = True
            args = args[1:]

        if len(args) > 0:
            kernel_name = args[0]
            self._initialize_buffer(kernel_name, shared=shared)
        else:
            PROMPT = "Select the kernel to launch:"
            available_kernels = [(x, False) for x in get_available_kernels()]
            running_kernels = [(x, True) for x in self.molten_kernels.keys()]

            if shared:
                available_kernels = []

            kernels = available_kernels + running_kernels
            if len(kernels) == 0:
                notify_error(
                    self.nvim, f"Unable to find any {'shared' if shared else ''}kernels to launch."
                )
                return

            chosen_kernel = kernels[0][0]
            self._initialize_buffer(chosen_kernel, shared=shared)

    def _deinit_buffer(self, molten_kernels: List[MoltenKernel]) -> None:
        # Have to copy this to get around reference issues
        for kernel in [x for x in molten_kernels]:
            kernel.deinit()
            for buf in kernel.buffers:
                self.buffers[buf.number].remove(kernel)
                if len(self.buffers[buf.number]) == 0:
                    del self.buffers[buf.number]
            del self.molten_kernels[kernel.kernel_id]

    def _do_evaluate_expr(self, kernel_name: str, expr):
        self._initialize_if_necessary()

        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        kernel = None
        for k in kernels:
            if k.kernel_id == kernel_name:
                kernel = k
                break
        if kernel is None:
            raise MoltenException(f"Kernel {kernel_name} not found")

        bufno = self.nvim.current.buffer.number
        cell = CodeCell(
            self.nvim,
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, 0, 0),
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, 0, 0, right_gravity=True),
        )

        kernel.run_code(expr, cell)

    def _get_sorted_buf_cells(self, kernels: List[MoltenKernel], bufnr: int) -> List[CodeCell]:
        return sorted([x for x in chain(*[k.outputs.keys() for k in kernels]) if x.bufno == bufnr])


    @pynvim.command("SaveIPYNB", nargs="*", sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_save_ipynb(self, args: List[str]) -> None:
        filename = self.nvim.current.buffer.name

        if not filename.endswith(".ipynb_interpreted"):
            self.nvim.command("echoerr 'Not an interpreted .ipynb file'")
            return

        try:
            # Reconstruct original .ipynb path
            checkpoint_dir = os.path.dirname(filename)
            basename = os.path.basename(filename).replace(".ipynb_interpreted", ".ipynb")
            original_ipynb_path = os.path.join(os.path.dirname(checkpoint_dir), basename)

            with open(original_ipynb_path, "r", encoding="utf-8") as f:
                nb_data = json.load(f)

            # Parse buffer contents
            cells = []
            cell_lines = []
            in_cell = False

            for line in self.nvim.current.buffer:
                if line.strip() == "<cell>":
                    in_cell = True
                    cell_lines = []
                elif line.strip() == "</cell>":
                    in_cell = False
                    # Always write as code cell
                    cells.append({
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {},
                        "outputs": [],
                        "source": [l for l in cell_lines]
                    })
                elif in_cell:
                    cell_lines.append(line)

            # Update and save
            nb_data["cells"] = cells
            with open(original_ipynb_path, "w", encoding="utf-8") as f:
                json.dump(nb_data, f, indent=2)

            self.nvim.command(f"echom 'Saved changes to {original_ipynb_path}'")

        except Exception as e:
            self.nvim.command(f"echoerr 'Failed to save notebook: {e}'")

    @pynvim.command("MoltenDeinit", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_deinit(self) -> None:
        self._initialize_if_necessary()

        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        self._clear_interface()

        self._deinit_buffer(kernels)

    @pynvim.command("VolcanoInfo", nargs=0, sync=True)  # type: ignore
    @nvimui  # type: ignore
    def command_info(self) -> None:
        create_info_window(self.nvim, self.molten_kernels, self.buffers, self.initialized)

    def _do_evaluate(self, kernel_name: str, pos: Tuple[Tuple[int, int], Tuple[int, int]]) -> None:
        self._initialize_if_necessary()

        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        kernel = None
        for k in kernels:
            if k.kernel_id == kernel_name:
                kernel = k
                break
        if kernel is None:
            raise MoltenException(f"Kernel {kernel_name} not found")

        bufno = self.nvim.current.buffer.number
        span = CodeCell(
            self.nvim,
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[0]),
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, *pos[1], right_gravity=True),
        )

        code = span.get_text(self.nvim)

        # delete overlapping cells from other kernels. Maintains the invariant that all code cells
        # from different kernels are disjoint
        for k in kernels:
            if k.kernel_id != kernel.kernel_id:
                if not k.try_delete_overlapping_cells(span):
                    return

        kernel.run_code(code, span)

    @pynvim.function("MoltenUpdateOption", sync=True) 
    @nvimui  # type: ignore
    def function_update_option(self, args) -> None:
        self._initialize_if_necessary()

        if len(args) == 2:
            option, value = args
            self.options.update_option(option, value)
        else:
            notify_error(
                self.nvim,
                f"Wrong number of arguments passed to :MoltenUpdateOption, expected 2, given {len(args)}",
            )

    @pynvim.function("MoltenAvailableKernels", sync=True)
    def function_available_kernels(self, _):
        """List of string kernel names that molten knows about"""
        return get_available_kernels()

    @pynvim.function("MoltenRunningKernels", sync=True)  # type: ignore
    def function_list_running_kernels(self, args: List[Optional[bool]]) -> List[str]:
        """List all the running kernels. When passed [True], returns only buf local kernels"""
        if not self.initialized:
            return []
        if len(args) > 0 and args[0]:
            buf = self.nvim.current.buffer.number
            if buf not in self.buffers:
                return []
            return [x.kernel_id for x in self.buffers[buf]]
        return list(self.molten_kernels.keys())

    @pynvim.function("MoltenStatusLineKernels", sync=True) 
    def function_status_line_kernels(self, args) -> str:
        kernels = self.function_list_running_kernels(args)
        return " ".join(kernels)

    @pynvim.function("MoltenStatusLineInit", sync=True) 
    def function_status_line_init(self, _) -> str:
        if self.initialized:
            return "Molten"
        return ""

    @pynvim.command("MoltenNext", sync=True, nargs="*") 
    @nvimui
    def command_next(self, args: List[str]) -> None:
        count = 1
        if len(args) > 0:
            try:
                count = int(args[0])
            except ValueError:
                count = 1

        c = self.nvim.api.win_get_cursor(0)
        bufnr = self.nvim.current.buffer.number
        pos = Position(bufnr, c[0] - 1, c[1])
        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        all_cells = self._get_sorted_buf_cells(kernels, bufnr)

        starting_index = None
        match all_cells:
            case [first, *_] if pos < first.begin:
                starting_index = 0
                if count > 0:
                    count -= 1
            case [*_, last] if last.end < pos:
                starting_index = len(all_cells) - 1
                if count < 0:
                    count += 1
            case _:
                for i, cell in enumerate(all_cells):
                    if pos in cell or (
                        i <= len(all_cells) - 2 and pos < all_cells[i + 1].begin and cell.end < pos
                    ):
                        starting_index = i

        if starting_index is not None:
            target_idx = (starting_index + count) % len(all_cells)
            target_pos = all_cells[target_idx].begin
            self.nvim.api.win_set_cursor(0, (target_pos.lineno + 1, target_pos.colno))
        else:
            notify_warn(self.nvim, "No cells to jump to")

    @pynvim.command("MoltenGoto", sync=True, nargs="*") 
    @nvimui
    def command_goto(self, args: List[str]) -> None:
        count = 1
        if len(args) > 0:
            try:
                count = int(args[0])
            except ValueError:
                count = 1

        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        all_cells = self._get_sorted_buf_cells(kernels, self.nvim.current.buffer.number)
        if len(all_cells) == 0:
            notify_warn(self.nvim, "No cells to jump to")
            return

        target_pos = all_cells[(count - 1) % len(all_cells)].begin
        self.nvim.api.win_set_cursor(0, (target_pos.lineno + 1, target_pos.colno))

    @pynvim.command("MoltenPrev", sync=True, nargs="*") 
    @nvimui
    def command_prev(self, args: List[str]) -> None:
        count = -1
        if len(args) > 0:
            try:
                count = -int(args[0])
            except ValueError:
                count = -1
        self.command_next([str(count)])

    @pynvim.command("MoltenEnterOutput", sync=True) 
    @nvimui  # type: ignore
    def command_enter_output_window(self) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        # We can do this iff we ensure that different kernels don't contain code cells that overlap
        for kernel in molten_kernels:
            kernel.enter_output()

    @pynvim.command("MoltenOpenInBrowser", sync=True)  
    @nvimui  # type: ignore
    def command_open_in_browser(self) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        for kernel in molten_kernels:
            if kernel.open_in_browser():
                notify_info(self.nvim, "Opened in browser")
                return

    @pynvim.command("MoltenImagePopup", sync=True)
    @nvimui  # type: ignore
    def command_image_popup(self) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        for kernel in molten_kernels:
            if kernel.open_image_popup():
                return

    @pynvim.command("MoltenEvaluateArgument", nargs="*", sync=True)
    @nvimui
    def commnand_molten_evaluate_argument(self, args: List[str]) -> None:
        if len(args) > 0 and args[0] in map(
            lambda x: x.kernel_id, self.buffers[self.nvim.current.buffer.number]
        ):
            self._do_evaluate_expr(args[0], " ".join(args[1:]))
        else:
            self.kernel_check(
                f"MoltenEvaluateArgument %k {' '.join(args)}", self.nvim.current.buffer
            )

    @pynvim.command("MoltenEvaluateVisual", nargs="*", sync=True)
    @nvimui  # type: ignore
    def command_evaluate_visual(self, args) -> None:
        if len(args) > 0:
            kernel = args[0]
        else:
            self.kernel_check("MoltenEvaluateVisual %k", self.nvim.current.buffer)
            return
        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'<")
        _, lineno_end, colno_end, _ = self.nvim.funcs.getpos("'>")

        if lineno_begin == 0 or colno_begin == 0 or lineno_end == 0 or colno_end == 0:
            notify_error(self.nvim, "No visual selection found")
            return

        span = (
            (
                lineno_begin - 1,
                min(colno_begin, len(self.nvim.funcs.getline(lineno_begin))) - 1,
            ),
            (
                lineno_end - 1,
                min(colno_end, len(self.nvim.funcs.getline(lineno_end))),
            ),
        )

        self._do_evaluate(kernel, span)

    @pynvim.function("MoltenEvaluateRange", sync=True) 
    @nvimui  # type: ignore
    def evaulate_range(self, args) -> None:
        start_col, end_col = 1, 0
        kernel = None
        span = args
        if type(args[0]) == str:
            kernel = args[0]
            span = args[1:]

        if len(span) == 2:
            start_line, end_line = span
        elif len(span) == 4:
            start_line, end_line, start_col, end_col = span
        else:
            notify_error(self.nvim, f"Invalid args passed to MoltenEvaluateRange. Got: {args}")
            return

        if not kernel:
            self.kernel_check(
                f"call MoltenEvaluateRange('%k', {start_line}, {end_line}, {start_col}, {end_col})",
                self.nvim.current.buffer,
            )
            return

        span = (
            (start_line - 1, start_col - 1),
            (end_line - 1, end_col - 1),
        )

        self._do_evaluate(kernel.strip(), span)

    @pynvim.command("MoltenEvaluateOperator", sync=True) 
    @nvimui  # type: ignore
    def command_evaluate_operator(self) -> None:
        self._initialize_if_necessary()

        self.nvim.options["operatorfunc"] = "MoltenOperatorfunc"
        self.nvim.feedkeys("g@")

    @pynvim.command("VolcanoEvaluate", nargs="*", sync=True)
    @nvimui
    def command_volcano_evaluate(self, args: List[str]) -> None:
        self._evaluate_cell()

    @pynvim.command("VolcanoEvaluateAll", nargs="*", sync=True)
    @nvimui
    def command_volcano_evaluate_all(self, args: List[str]) -> None:
        self._evaluate_all_cells()

    @pynvim.command("VolcanoEvaluateJump", nargs="*", sync=True)
    @nvimui
    def command_volcano_evaluate_jump(self, args: List[str]) -> None:
        self._evaluate_cell() 

        buf = self.nvim.current.buffer
        win = self.nvim.current.window
        cur_line = self.nvim.funcs.line('.') - 1
        total_lines = len(buf)

        # Find current cell
        active_block = None
        cell_blocks = []
        i = 0
        while i < total_lines:
            if buf[i].strip() == "<cell>":
                start = i
                i += 1
                while i < total_lines and buf[i].strip() != "</cell>":
                    i += 1
                if i < total_lines:
                    end = i
                    cell_blocks.append((start, end))
                    if start <= cur_line <= end:
                        active_block = (start, end)
            i += 1

        if not active_block:
            return

        _, end_line = active_block

        # Move to next cell or create one
        def _move_cursor_after_output():
            buf_lines = buf[:]
            for i in range(end_line + 1, len(buf_lines)):
                if buf_lines[i].strip() == "<cell>":
                    win.cursor = (i + 1, 0)
                    return

            # No next cell — insert one with spacing
            insert_line = len(buf_lines)

            # Add a newline if the last line isn't empty
            if buf_lines and buf_lines[-1].strip() != "":
                buf.api.set_lines(insert_line, insert_line, False, [""])
                insert_line += 1

            new_cell = ["<cell>", "", "</cell>"]
            buf.api.set_lines(insert_line, insert_line, False, new_cell)
            win.cursor = (insert_line + 1, 0)  # Inside new cell


        self.nvim.async_call(_move_cursor_after_output)

    @pynvim.command("VolcanoEvaluateAbove", nargs="*", sync=True)
    @nvimui
    def command_volcano_evaluate_above(self, args: List[str]) -> None:
        buf_obj = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0]

        if '<output>' in buf_obj[cursor_row - 1]:
            if cursor_row < len(buf_obj):
                cursor_row += 1
                self.nvim.current.window.cursor = (cursor_row, 0)

        new_buf = self._clean_output_blocks(buf_obj[0:cursor_row - 1])

        if new_buf == False:
            while new_buf == False:
                if cursor_row < len(buf_obj):
                    cursor_row += 1
                    self.nvim.current.window.cursor = (cursor_row, 0)
                    new_buf = self._clean_output_blocks(buf_obj[0:cursor_row - 1])
                else:
                    break

        if new_buf != False:
            buf_obj[0:cursor_row - 1] = self._clean_output_blocks(buf_obj[0:cursor_row - 1])

        buf = buf_obj[:]
        win = self.nvim.current.window
        cursor_row = win.cursor[0]

        cell_instance = 0
        def run():
            cell_instance = 0
            first_cell = True
            offset = 0

            for line in range(cursor_row):
                if buf[line].strip() == "<cell>":
                    target_line = line + offset
                    if first_cell:
                        self.nvim.async_call(lambda line=target_line: (
                            self._move_cursor_to(win, line),
                            self._evaluate_cell(delay=True)
                        ))
                        first_cell = False
                    else:
                        self.nvim.async_call(lambda line=target_line: (
                            self._move_cursor_to(win, line),
                            self._evaluate_cell()
                        ))
                    offset += 4
                    time.sleep(0.01)


        threading.Thread(target=run, daemon=True).start()

    @pynvim.command("VolcanoEvaluateBelow", nargs="*", sync=True)
    @nvimui
    def command_volcano_evaluate_below(self, args: List[str]) -> None:
        buf_obj = self.nvim.current.buffer
        win = self.nvim.current.window
        cursor_row = win.cursor[0]

        buf_obj[cursor_row:] = self._clean_output_blocks(buf_obj[cursor_row:])

        buf = buf_obj[:]
        cursor_line = win.cursor[0] - 1 

        def run():
            cell_lines = []
            for i in range(cursor_line, len(buf)):
                if buf[i].strip() == "<cell>":
                    cell_lines.append(i)

            first_cell = True
            first_offset = 4
            offset_accumulator = 0 

            for cell_line in cell_lines:
                adjusted_line = cell_line + offset_accumulator

                if first_cell:
                    self.nvim.async_call(lambda l=adjusted_line: setattr(win, "cursor", (l + 1, 0)))
                    self.nvim.async_call(lambda: self._evaluate_cell(delay=True))
                    first_cell = False
                else:
                    self.nvim.async_call(lambda l=adjusted_line + first_offset: setattr(win, "cursor", (l + 1, 0)))
                    self.nvim.async_call(lambda: self._evaluate_cell())
                    offset_accumulator += first_offset

                time.sleep(0.01)

        threading.Thread(target=run, daemon=True).start()

    @pynvim.command("VolcanoDeleteOutput", nargs="*", sync=True)
    @nvimui
    def command_volcano_delete_output(self, args: List[str]) -> None:
        nvim = self.nvim
        buf = nvim.current.buffer
        cursor_line = nvim.current.window.cursor[0] - 1
        lines = buf[:]

        start_idx = None
        for i in range(cursor_line, len(lines)):
            if lines[i].strip() == "<output>":
                start_idx = i
                break
        if start_idx is None:
            return

        end_idx = None
        for j in range(start_idx + 1, len(lines)):
            if lines[j].strip() == "</output>":
                end_idx = j
                break
        if end_idx is None:
            return

        if end_idx + 1 < len(lines) and lines[end_idx + 1].strip() == "":
            end_idx += 1

        block_lines = lines[start_idx:end_idx + 1]
        cleaned_block = self._clean_output_blocks(block_lines)
        buf.api.set_lines(start_idx, end_idx + 1, False, cleaned_block)

    @pynvim.command("VolcanoDeleteAllOutputs", nargs="*", sync=True)
    @nvimui
    def command_volcano_delete_all_outputs(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        buf[:] = self._clean_output_blocks(buf[:])
        buf.api.set_lines(len(buf), len(buf), False, [""])

    @pynvim.command("VolcanoDeleteOutputsAbove", nargs="*", sync=True)
    @nvimui
    def command_volcano_delete_outputs_above(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0]

        if '<output>' in buf[cursor_row - 1]:
            if cursor_row < len(buf): 
                cursor_row += 1
                self.nvim.current.window.cursor = (cursor_row, 0)

        new_buf = self._clean_output_blocks(buf[0:cursor_row - 1])

        if new_buf == False:
            while new_buf == False:
                if cursor_row < len(buf):
                    cursor_row += 1
                    self.nvim.current.window.cursor = (cursor_row, 0)
                    new_buf = self._clean_output_blocks(buf[0:cursor_row - 1])
                else:
                    break

        if new_buf != False:
            buf[0:cursor_row - 1] = self._clean_output_blocks(buf[0:cursor_row - 1])

    @pynvim.command("VolcanoDeleteOutputsBelow", nargs="*", sync=True)
    @nvimui
    def command_volcano_delete_outputs_below(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        win = self.nvim.current.window
        cursor_row = self.nvim.current.window.cursor[0]

        for line in range(cursor_row, len(buf) + 1):
            if buf[line - 1].strip() == "<cell>":
                cursor_row = line - 1
                break
            elif buf[line - 1].strip() == "</cell>":
                back_line = line - 2
                while back_line >= 0:
                    if back_line == 1:
                        cursor_row = 1
                        break
                    elif buf[back_line].strip() == "<cell>":
                        cursor_row = back_line
                        break
                    back_line -= 1
                break

        win.cursor = (cursor_row, 0)

        buf = self.nvim.current.buffer
        win = self.nvim.current.window
        cursor_row = self.nvim.current.window.cursor[0]

        buf[cursor_row:] = self._clean_output_blocks(buf[cursor_row:])

    @pynvim.command("VolcanoSwitchCellTypeForward", nargs="*", sync=True)
    @nvimui
    def command_volcano_switch_cell_type_forward(self, args: List[str]) -> None:
        self._switch_cell_type(direction="forward")

    @pynvim.command("VolcanoSwitchCellTypeBackward", nargs="*", sync=True)
    @nvimui
    def command_volcano_switch_cell_type_backward(self, args: List[str]) -> None:
        self._switch_cell_type(direction="backward")


    @pynvim.command("VolcanoCreateCellUpward", nargs="*", sync=True)
    @nvimui
    def command_volcano_create_cell_upward(self, args: List[str]) -> None:
        self._create_cell(direction="upward")

    @pynvim.command("VolcanoCreateCellDownward", nargs="*", sync=True)
    @nvimui
    def command_volcano_create_cell_downward(self, args: List[str]) -> None:
        self._create_cell(direction="downward")

    @pynvim.command("VolcanoMoveCellUpward", nargs="*", sync=True)
    @nvimui
    def command_volcano_move_cell_upward(self, args: List[str]) -> None:
        self._move_cell("upward")

    @pynvim.command("VolcanoMoveCellDownward", nargs="*", sync=True)
    @nvimui
    def command_volcano_move_cell_downward(self, args: List[str]) -> None:
        self._move_cell("downward")

    @pynvim.command("VolcanoDeleteCell", nargs="*", sync=True)
    @nvimui
    def command_volcano_delete_cell(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0] - 1  # 0-based
        total_lines = len(buf)

        def find_tag_block(start_row: int, open_tag: str, close_tag: str):
            """Find the inclusive range for a tag block surrounding the cursor."""
            open_idx, close_idx = None, None

            # Search upward for opening tag
            for i in range(start_row, -1, -1):
                if buf[i].strip() == open_tag:
                    open_idx = i
                    break

            # Search downward for closing tag
            for j in range(start_row, total_lines):
                if buf[j].strip() == close_tag:
                    close_idx = j
                    break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None

        def find_output_block(after_row: int):
            """Find next <output> block (if any) after a given line index."""
            open_idx, close_idx = None, None

            for i in range(after_row + 1, total_lines):
                if buf[i].strip() == "<output>":
                    open_idx = i
                    break

            if open_idx is not None:
                for j in range(open_idx, total_lines):
                    if buf[j].strip() == "</output>":
                        close_idx = j
                        break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None

        def delete_range(start: int, end: int):
            """Delete inclusive range and clean up extra blank lines."""
            del buf[start:end + 1]

            # Remove stray blank line before
            if start - 1 >= 0 and buf[start - 1].strip() == "":
                del buf[start - 1]
                start -= 1

            # Remove stray blank line after (if not EOF)
            if start < len(buf) and buf[start].strip() == "":
                del buf[start]

        def run():
            cell_block = find_tag_block(cursor_row, "<cell>", "</cell>")
            if not cell_block:
                self.nvim.err_write("No <cell> block found under cursor.\n")
                return

            cell_start, cell_end = cell_block
            output_block = find_output_block(cell_end)

            # Delete output block first (if exists)
            if output_block:
                delete_range(output_block[0], output_block[1])

            # Adjust in case lines shifted
            new_total = len(buf)
            shift = (total_lines - new_total)
            if shift > 0 and output_block and output_block[0] < cell_start:
                cell_start -= shift
                cell_end -= shift

            delete_range(cell_start, cell_end)

            # Reposition cursor safely
            new_cursor = min(cell_start, len(buf) - 1)
            self.nvim.current.window.cursor = (new_cursor + 1, 0)

        self.nvim.async_call(run)

    @pynvim.command("VolcanoCopyCell", nargs="*", sync=True)
    @nvimui
    def command_volcano_copy_cell(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0] - 1  # 0-based
        total_lines = len(buf)

        def find_tag_block(start_row: int, open_tag: str, close_tag: str):
            """Find inclusive range for a tag block surrounding the cursor."""
            open_idx, close_idx = None, None

            # Search upward for <cell>
            for i in range(start_row, -1, -1):
                if buf[i].strip() == open_tag:
                    open_idx = i
                    break

            # Search downward for </cell>
            for j in range(start_row, total_lines):
                if buf[j].strip() == close_tag:
                    close_idx = j
                    break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None

        def find_output_block(after_row: int):
            """Find next <output> block (if any) after a given line index."""
            open_idx, close_idx = None, None

            for i in range(after_row + 1, total_lines):
                if buf[i].strip() == "<output>":
                    open_idx = i
                    break

            if open_idx is not None:
                for j in range(open_idx, total_lines):
                    if buf[j].strip() == "</output>":
                        close_idx = j
                        break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None


        def run():
            cell_block = find_tag_block(cursor_row, "<cell>", "</cell>")
            if not cell_block:
                self.nvim.err_write("No <cell> block found under cursor.\n")
                return

            cell_start, cell_end = cell_block
            final_start, final_end = cell_start, cell_end

            # Search for <output> directly after *this* cell
            next_line = cell_end + 1
            while next_line < total_lines and buf[next_line].strip() == "":
                # skip blank lines between </cell> and <output>
                next_line += 1

            if next_line < total_lines and buf[next_line].strip() == "<output>":
                output_block = find_tag_block(next_line, "<output>", "</output>")
                if output_block:
                    final_end = output_block[1]

            # Move cursor to start
            self.nvim.command(f"normal! {final_start + 1}G")
            # Enter Visual Line mode
            self.nvim.command("normal! V")
            # Move down to end line
            if final_end > final_start:
                self.nvim.command(f"normal! {final_end - final_start}j")
            # Yank selection
            self.nvim.command("normal! y")

        self.nvim.async_call(run)

    @pynvim.command("VolcanoPasteCell", nargs="*", sync=True)
    @nvimui
    def command_volcano_Paste_cell(self, args: List[str]) -> None:
        buf = self.nvim.current.buffer
        cursor_row = self.nvim.current.window.cursor[0] - 1  # 0-based
        total_lines = len(buf)

        def find_tag_block(start_row: int, open_tag: str, close_tag: str):
            """Find inclusive range for a tag block surrounding the cursor."""
            open_idx, close_idx = None, None

            # Search upward for opening tag
            for i in range(start_row, -1, -1):
                if buf[i].strip() == open_tag:
                    open_idx = i
                    break

            # Search downward for closing tag
            for j in range(start_row, total_lines):
                if buf[j].strip() == close_tag:
                    close_idx = j
                    break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None

        def find_output_block(after_row: int):
            """Find the next <output> block after a given line index."""
            open_idx, close_idx = None, None

            for i in range(after_row + 1, total_lines):
                if buf[i].strip() == "<output>":
                    open_idx = i
                    break

            if open_idx is not None:
                for j in range(open_idx, total_lines):
                    if buf[j].strip() == "</output>":
                        close_idx = j
                        break

            if open_idx is not None and close_idx is not None:
                return (open_idx, close_idx)
            return None

        def run():
            try:
                yank_data = self.nvim.eval('@"')  # unnamed register
            except Exception:
                self.nvim.err_write("Failed to read clipboard register.\n")
                return

            if not yank_data or not isinstance(yank_data, str):
                self.nvim.err_write("Clipboard is empty or invalid.\n")
                return

            yank_lines = yank_data.split("\n")
            if not any("<cell>" in line for line in yank_lines):
                self.nvim.err_write("Clipboard does not contain a <cell> block.\n")
                return

            # Find the current cell block
            cell_block = find_tag_block(cursor_row, "<cell>", "</cell>")
            if not cell_block:
                self.nvim.err_write("No <cell> block found under cursor.\n")
                return

            cell_start, cell_end = cell_block
            insert_after = cell_end

            # If the current cell has an output, insert after it
            output_block = find_output_block(cell_end)
            if output_block:
                insert_after = output_block[1]

            insert_row = insert_after + 1  # position just after current block

            # Prepare lines: add one blank line above
            insert_content = [""] + yank_lines
            if not insert_content[-1].strip():
                insert_content = insert_content[:-1]  # remove trailing empty line

            # Insert new block
            buf[insert_row:insert_row] = insert_content

            # Move cursor to start of newly inserted block
            self.nvim.command(f"normal! {insert_row + 1}G")

        self.nvim.async_call(run)

    @pynvim.command("MoltenReevaluateAll", nargs=0, sync=True) 
    @nvimui  # type: ignore
    def command_reevaluate_all(self) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        for kernel in molten_kernels:
            kernel.reevaluate_all()

    @pynvim.command("MoltenReevaluateCell", nargs=0, sync=True) 
    @nvimui  # type: ignore
    def command_evaluate_cell(self) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        in_cell = False
        for kernel in molten_kernels:
            if kernel.reevaluate_cell():
                in_cell = True

        if not in_cell:
            notify_error(self.nvim, "Not in a cell")

    @pynvim.command("VolcanoInterrupt", nargs="*", sync=True)
    @nvimui  # type: ignore
    def command_interrupt(self, args) -> None:
        """Interrupt the currently running evaluation without clearing namespaces or counters."""
        # terminate any running evaluation process
        if self.current_eval_process and self.current_eval_process.is_alive():
            pid = self.current_eval_pid
            try:
                os.kill(pid, signal.SIGKILL)
                self.current_eval_process.join(timeout=1)
            except ProcessLookupError:
                pass
            except Exception:
                pass
            # flag for _evaluate_and_update to mark Kernel_Interrupted
            self.eval_interrupted = True

        # reset process tracking
        self.current_eval_process = None
        self.current_eval_pid = None
        self.current_eval_bufnr = None

        # drain evaluation queue but keep counters and globals
        with self.eval_lock:
            while not self.eval_queue.empty():
                try:
                    self.eval_queue.get_nowait()
                except queue.Empty:
                    break

        # update queued cells to reflect that they were interrupted
        try:
            for buf in self.nvim.buffers:
                lines = list(buf[:])
                changed = False
                for i, line in enumerate(lines):
                    # replace queued status indicator with Kernel_Interrupted
                    if "[*]" in line and "queue" in line:
                        idx = line.find("[*]")
                        prefix = line[:idx]
                        lines[i] = f"{prefix}[Kernel_Interrupted]"
                        changed = True
                if changed:
                    buf[:] = lines
        except Exception:
            pass

    @pynvim.command("VolcanoRestart", nargs="*", sync=True, bang=True)
    @nvimui  # type: ignore
    def command_restart(self, args, bang) -> None:
        self._restart_kernel()

    @pynvim.command("VolcanoRestartAndDeleteAllOutput", nargs="*", sync=True, bang=True)
    @nvimui  # type: ignore
    def command_restart_delete_all_output(self, args, bang) -> None:
        buf_obj = self.nvim.current.buffer
        win = self.nvim.current.window
        win.cursor = (1, 0)
        cursor_row = self.nvim.current.window.cursor[0]
        self._restart_kernel()
        buf_obj[cursor_row:] = self._clean_output_blocks(buf_obj[cursor_row:])

    @pynvim.command("VolcanoRestartAndEvaluateAll", nargs="*", sync=True, bang=True)
    @nvimui  # type: ignore
    def command_restart_evaluate_all(self, args, bang) -> None:
        self._restart_kernel()
        self._evaluate_all_cells()

    @pynvim.command("VolcanoRestartAndEvaluateUpToCursor", nargs="*", sync=True, bang=True)
    @nvimui  # type: ignore
    def command_restart_evaluate_up_to_cursor(self, args, bang) -> None:
        buf_obj = self.nvim.current.buffer
        win = self.nvim.current.window
        cursor_row = self.nvim.current.window.cursor[0]
        self._restart_kernel()
        self._evaluate_all_cells(cursor_row)

    @pynvim.command("MoltenDelete", nargs=0, sync=True, bang=True) 
    @nvimui  # type: ignore
    def command_delete(self, bang) -> None:
        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        for molten in molten_kernels:
            if bang:
                # Delete all cells in current buffer
                molten.clear_buffer(self.nvim.current.buffer.number)
            elif molten.selected_cell is not None:
                molten.delete_current_cell()
                return

    @pynvim.command("MoltenShowOutput", nargs=0, sync=True) 
    @nvimui  # type: ignore
    def command_show_output(self) -> None:
        self._initialize_if_necessary()

        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None

        for molten in molten_kernels:
            if molten.current_output is not None:
                molten.should_show_floating_win = True
                self._update_interface()
                return

    @pynvim.command("MoltenHideOutput", nargs=0, sync=True) 
    @nvimui  # type: ignore
    def command_hide_output(self) -> None:
        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            # get the current buffer, and then search for it in all molten buffers
            cur_buf = self.nvim.current.buffer
            for moltenbuf in self.buffers.values():
                # if we find it, then we know this is a molten output, and we can safely quit and
                # call hide to hide it
                output_windows = map(
                    lambda x: x.display_buf, chain(*[o.outputs.values() for o in moltenbuf])
                )
                if cur_buf in output_windows:
                    self.nvim.command("q")
                    self.nvim.command(":MoltenHideOutput")
                    return
            return

        for molten in molten_kernels:
            molten.should_show_floating_win = False

        self._update_interface()

    @pynvim.command("MoltenImportOutput", nargs="*", sync=True)
    @nvimui  # type: ignore
    def command_import(self, args) -> None:
        self._initialize_if_necessary()

        buf = self.nvim.current.buffer
        if len(args) > 0:
            path = args[0]
        else:
            path = get_default_import_export_file(self.nvim, buf)

        if len(args) > 1:
            kernel = args[1]
        else:
            path = path.replace("%k", r"\%k")
            self.kernel_check(f"MoltenImportOutput {path} %k", buf)
            return

        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None
        for molten in kernels:
            if molten.kernel_id == kernel:
                import_outputs(self.nvim, molten, path)
                break

    @pynvim.command("MoltenExportOutput", nargs="*", sync=True, bang=True)
    @nvimui  # type: ignore
    def command_export(self, args, bang: bool) -> None:
        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        buf = self.nvim.current.buffer
        if len(args) > 0:
            path = args[0]
        else:
            path = get_default_import_export_file(self.nvim, buf)

        if len(args) > 1:
            kernel = args[1]
        else:
            path = path.replace("%k", r"\%k")
            self.kernel_check(f"MoltenExportOutput{'!' if bang else ''} {path} %k", buf)
            return

        for molten in kernels:
            if molten.kernel_id == kernel:
                export_outputs(self.nvim, molten, path, bang)
                break

    @pynvim.command("MoltenSave", nargs="*", sync=True) 
    @nvimui  # type: ignore
    def command_save(self, args) -> None:
        kernels = self._get_current_buf_kernels(True)
        assert kernels is not None

        buf = self.nvim.current.buffer
        if len(args) > 0:
            path = args[0]
        else:
            path = get_default_save_file(self.options, buf)

        if len(args) > 1:
            kernel = args[1]
        else:
            path = path.replace("%k", r"\%k")
            self.kernel_check(f"MoltenSave {path} %k", buf)
            return

        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        for molten in kernels:
            if molten.kernel_id == kernel:
                with open(path, "w") as file:
                    json.dump(save(molten, buf.number), file)
                break
        notify_info(self.nvim, f"Saved kernel `{kernel}` to: {path}")

    @pynvim.command("MoltenLoad", nargs="*", sync=True) 
    @nvimui  # type: ignore
    def command_load(self, args) -> None:
        self._initialize_if_necessary()

        shared = False

        if len(args) > 0 and args[0] == "shared":
            shared = True
            args = args[1:]

        if len(args) > 0:
            path = args[0]
        else:
            path = get_default_save_file(self.options, self.nvim.current.buffer)

        if self.nvim.current.buffer.number in self.buffers:
            raise MoltenException(
                "Molten is already initialized for this buffer; MoltenLoad initializes Molten."
            )

        with open(path) as file:
            data = json.load(file)

        molten = None

        try:
            notify_info(self.nvim, f"Attempting to load from: {path}")

            MoltenIOError.assert_has_key(data, "version", int)
            if (version := data["version"]) != 1:
                raise MoltenIOError(f"Bad version: {version}")

            MoltenIOError.assert_has_key(data, "kernel", str)
            kernel_name = data["kernel"]

            molten = self._initialize_buffer(kernel_name, shared=shared)
            if molten:
                load(self.nvim, molten, self.nvim.current.buffer, data)

                self._update_interface()
        except MoltenIOError as err:
            if molten is not None:
                self._deinit_buffer([molten])

            raise MoltenException("Error while doing Molten IO: " + str(err))

    # Internal functions which are exposed to VimScript

    @pynvim.function("MoltenBufLeave", sync=True) 
    @nvimui  # type: ignore
    def function_clear_interface(self, _: List[Any]) -> None:
        self._clear_on_buf_leave()

    @pynvim.function("MoltenOnBufferUnload", sync=True) 
    @nvimui  # type: ignore
    def function_on_buffer_unload(self, _: Any) -> None:
        abuf_str = self.nvim.funcs.expand("<abuf>")
        if not abuf_str:
            return

        molten = self.buffers.get(int(abuf_str))
        if molten is None:
            return

        self._deinit_buffer(molten)

    @pynvim.function("MoltenOnExitPre", sync=True) 
    @nvimui  # type: ignore
    def function_on_exit_pre(self, _: Any) -> None:
        self._deinitialize()

    @pynvim.function("MoltenTick", sync=True) 
    @nvimui  # type: ignore
    def function_molten_tick(self, _: Any) -> None:
        self._initialize_if_necessary()

        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            return

        for m in molten_kernels:
            m.tick()

    @pynvim.function("MoltenTickInput", sync=False) 
    @nvimui  # type: ignore
    def function_molten_tick_input(self, _: Any) -> None:
        self._initialize_if_necessary()

        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            return

        for m in molten_kernels:
            m.tick_input()

    @pynvim.function("MoltenSendStdin", sync=False) 
    @nvimui  # type: ignore
    def function_molten_send_stdin(self, args: Tuple[str, str]) -> None:
        molten_kernels = self._get_current_buf_kernels(False)
        if molten_kernels is None:
            return

        for m in molten_kernels:
            if m.kernel_id == args[0]:
                m.send_stdin(args[1])

    @pynvim.function("MoltenUpdateInterface", sync=True) 
    @nvimui  # type: ignore
    def function_update_interface(self, _: Any) -> None:
        self._update_interface()

    @pynvim.function("MoltenOnCursorMoved", sync=True)
    @nvimui
    def function_on_cursor_moved(self, _) -> None:
        self._on_cursor_moved()

    @pynvim.function("MoltenOnWinScrolled", sync=True)
    @nvimui
    def function_on_win_scrolled(self, _) -> None:
        self._on_cursor_moved(scrolled=True)

    @pynvim.function("MoltenOperatorfunc", sync=True)
    @nvimui
    def function_molten_operatorfunc(self, args) -> None:
        if not args:
            return

        kind = args[0]

        _, lineno_begin, colno_begin, _ = self.nvim.funcs.getpos("'[")
        _, lineno_end, colno_end, _ = self.nvim.funcs.getpos("']")

        if kind == "line":
            colno_begin = 1
            colno_end = 0
        elif kind == "char":
            colno_begin = min(colno_begin, len(self.nvim.funcs.getline(lineno_begin)))
            colno_end = min(colno_end, len(self.nvim.funcs.getline(lineno_end))) + 1
        else:
            raise MoltenException(f"this kind of selection is not supported: '{kind}'")

        span = (
            (lineno_begin, colno_begin),
            (lineno_end, colno_end),
        )

        self.kernel_check(
            f"call MoltenEvaluateRange('%k', {span[0][0]}, {span[1][0]}, {span[0][1]}, {span[1][1]})",
            self.nvim.current.buffer,
        )

    @pynvim.function("MoltenDefineCell", sync=True)
    def function_molten_define_cell(self, args: List[int]) -> None:
        if not args:
            return

        molten_kernels = self._get_current_buf_kernels(True)
        assert molten_kernels is not None
        assert self.canvas is not None

        start = args[0]
        end = args[1]

        if len(args) == 3:
            kernel = args[2]
        elif len(self.buffers[self.nvim.current.buffer.number]) == 1:
            kernel = self.buffers[self.nvim.current.buffer.number][0].kernel_id
        else:
            raise MoltenException(
                "MoltenDefineCell called without a kernel argument while multiple kernels are active"
            )

        bufno = self.nvim.current.buffer.number
        span = CodeCell(
            self.nvim,
            DynamicPosition(self.nvim, self.extmark_namespace, bufno, start - 1, 0),
            DynamicPosition(
                self.nvim, self.extmark_namespace, bufno, end - 1, -1, right_gravity=True
            ),
        )

        for molten in molten_kernels:
            if molten.kernel_id == kernel:
                molten.outputs[span] = OutputBuffer(
                    self.nvim, self.canvas, molten.extmark_namespace, self.options
                )
                break

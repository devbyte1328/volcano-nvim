local Context = require('render-markdown.request.context')
local Decorator = require('render-markdown.lib.decorator')
local Extmark = require('render-markdown.lib.extmark')
local Range = require('render-markdown.lib.range')
local compat = require('render-markdown.lib.compat')
local env = require('render-markdown.lib.env')
local handlers = require('render-markdown.core.handlers')
local iter = require('render-markdown.lib.iter')
local log = require('render-markdown.core.log')
local state = require('render-markdown.state')
---@class render.md.ui.Config
---@field on render.md.on.Config
---@class render.md.Ui
---@field private config render.md.ui.Config
local M = {}
M.ns = vim.api.nvim_create_namespace('render-markdown.nvim')
M.suppress_ns = vim.api.nvim_create_namespace('render-markdown/suppress')
---@private
---@type table<integer, render.md.Decorator>
M.cache = {}
---called from state on setup
---@param config render.md.ui.Config
function M.setup(config)
    M.config = config
    -- clear marks and reset cache
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
        vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
        vim.api.nvim_buf_clear_namespace(buf, M.suppress_ns, 0, -1)
    end
    M.cache = {}
end
---@param buf integer
---@return render.md.Decorator
function M.get(buf)
    local result = M.cache[buf]
    if not result then
        result = Decorator.new(buf)
        M.cache[buf] = result
    end
    return result
end
---Used by fzf-lua: https://github.com/ibhagwan/fzf-lua/blob/main/lua/fzf-lua/previewer/builtin.lua
---@param buf integer
---@param win integer
---@param event string
---@param force boolean
function M.update(buf, win, event, force)
    log.buf('info', 'Update', buf, event, ('force %s'):format(force))
    M.updater.new(buf, win, force):start()
end
-- =========================
-- TAG-AWARE RENDER HELPERS
-- =========================
-- Returns:
-- pairs: { {s=<line>, e=<line>}, ... } -- inclusive, EXCLUDES tag lines
-- ranges: { Range.new(s, e), ... }
-- maxln: last buffer line (0-indexed)
-- start_cols: { col_after_tag, ... } 0-based column after <markdown> on tag line
local function markdown_inside_ranges(buf)
    local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
    local pairs, ranges, start_cols = {}, {}, {}
    local start_tag = nil
    local current_start_col = nil
    for i, line in ipairs(lines) do
        local row = i - 1
        if not start_tag then
            local pos = line:find("<markdown>")
            if pos then
                current_start_col = pos + 8 -- 0-based col after the tag (pos 1-based + 9 -1 = pos +8)
                start_tag = row
            end
        else
            local pos_close = line:find("</markdown>")
            if pos_close then
                local s = start_tag + 1
                local e = row - 1
                if s <= e then
                    table.insert(pairs, { s = s, e = e })
                    table.insert(ranges, Range.new(s, e))
                    table.insert(start_cols, current_start_col)
                end
                start_tag = nil
                current_start_col = nil
            end
        end
    end
    -- If an opening tag exists without a closing tag, render to EOF
    if start_tag then
        local s = start_tag + 1
        local e = #lines - 1
        if s <= e then
            table.insert(pairs, { s = s, e = e })
            table.insert(ranges, Range.new(s, e))
            table.insert(start_cols, current_start_col)
        end
    end
    return pairs, ranges, (#lines - 1), start_cols
end
-- Compute covered ranges for all blocks: cell, output, markdown, raw (inclusive tag to tag)
local function all_blocks_covered_ranges(buf)
    local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
    local covered = {}
    local current_start = nil
    local current_type = nil
    local tag_types = { cell = "cell", output = "output", markdown = "markdown", raw = "raw" }
    for i, line in ipairs(lines) do
        local row = i - 1
        -- Check for opening tag
        local open_type = nil
        for t, full in pairs(tag_types) do
            if line:find("<" .. full .. ">") then
                open_type = t
                break
            end
        end
        if open_type then
            current_start = row
            current_type = open_type
        else
            -- Check for closing tag
            if current_start and current_type and line:find("</" .. current_type .. ">") then
                table.insert(covered, { s = current_start, e = row })
                current_start = nil
                current_type = nil
            end
        end
    end
    -- If open without close, cover to EOF
    if current_start then
        table.insert(covered, { s = current_start, e = #lines - 1 })
    end
    return covered
end
-- Compute the complement (outside) of the covered ranges over [0, maxline]
local function outside_from_covered(covered, maxline)
    local outs = {}
    if #covered == 0 then
        table.insert(outs, { s = 0, e = maxline })
        return outs
    end
    table.sort(covered, function(a, b) return a.s < b.s end)
    local cursor = 0
    for _, p in ipairs(covered) do
        if p.s > cursor then
            table.insert(outs, { s = cursor, e = p.s - 1 })
        end
        cursor = p.e + 1
    end
    if cursor <= maxline then
        table.insert(outs, { s = cursor, e = maxline })
    end
    return outs
end
-- Paint outside ranges with Normal highlight (to suppress TS/syntax)
local function apply_syntax_suppression(buf, outs)
    local priority = 20000
    vim.api.nvim_buf_clear_namespace(buf, M.suppress_ns, 0, -1)
    for _, r in ipairs(outs) do
        if r.s <= r.e then
            vim.api.nvim_buf_set_extmark(buf, M.suppress_ns, r.s, 0, {
                end_row = r.e + 1, -- end_row is exclusive
                end_col = 0,
                hl_group = 'Normal',
                hl_eol = true,
                priority = priority,
            })
        end
    end
end
-- Turn inside line-pairs into Treesitter included range tuples
-- format: { start_row, start_col, end_row, end_col }
-- start_row is tag row, start_col after tag on tag line, to simulate leading blank line
local function ts_included_ranges_from_pairs(pairs, start_cols)
    local ir = {}
    for idx, p in ipairs(pairs) do
        local sc = start_cols[idx]
        local start_row = p.s - 1
        table.insert(ir, { start_row, sc, p.e + 1, 0 })
    end
    return ir
end
---@class render.md.ui.Updater
---@field private buf integer
---@field private win integer
---@field private force boolean
---@field private decorator render.md.Decorator
---@field private config render.md.buf.Config
---@field private mode string
local Updater = {}
Updater.__index = Updater
---@param buf integer
---@param win integer
---@param force boolean
---@return render.md.ui.Updater
function Updater.new(buf, win, force)
    local self = setmetatable({}, Updater)
    self.buf = buf
    self.win = win
    self.force = force
    self.decorator = M.get(buf)
    self.config = state.get(buf)
    return self
end
function Updater:start()
    if not env.valid(self.buf, self.win) then
        return
    end
    if env.buf.empty(self.buf) then
        return
    end
    self.decorator:schedule(
        self:changed(),
        self.config.debounce,
        log.runtime('update', function()
            self:run()
        end)
    )
end
---@private
---@return boolean
function Updater:changed()
    -- force or buffer has changed or we have not handled the visible range yet
    return self.force
        or self.decorator:changed()
        or not Context.contains(self.buf, self.win)
end
---@private
function Updater:run()
    if not env.valid(self.buf, self.win) then
        return
    end
    self.mode = env.mode.get() -- mode is only available after this point
    local bufname = vim.api.nvim_buf_get_name(self.buf)
    local is_ipynb = bufname ~= '' and bufname:match('%.ipynb_interpreted$')
    local render = self.config.enabled
        and self.config.resolved:render(self.mode)
        and not env.win.get(self.win, 'diff')
        and env.win.view(self.win).leftcol == 0
    log.buf('info', 'Render', self.buf, render)
    local next_state = render and 'rendered' or 'default'
    for _, win in ipairs(env.buf.windows(self.buf)) do
        for name, value in pairs(self.config.win_options) do
            env.win.set(win, name, value[next_state])
        end
    end
    if not render then
        self:clear()
    else
        self:render()
    end
    -- Always apply suppression for ipynb_interpreted files (gaps outside all blocks)
    if is_ipynb then
        local _, _, maxline = markdown_inside_ranges(self.buf)
        local all_covered = all_blocks_covered_ranges(self.buf)
        local outside_pairs = outside_from_covered(all_covered, maxline)
        apply_syntax_suppression(self.buf, outside_pairs)
    end
end
---@private
function Updater:clear()
    local extmarks = self.decorator:get()
    for _, extmark in ipairs(extmarks) do
        extmark:hide(M.ns, self.buf)
    end
    vim.api.nvim_buf_clear_namespace(self.buf, M.ns, 0, -1)
    M.config.on.clear({ buf = self.buf, win = self.win })
end
---@private
---@param callback fun(extmarks: render.md.Extmark[]|nil)
function Updater:parse(callback)
    local ok, parser = pcall(vim.treesitter.get_parser, self.buf)
    if ok and parser then
        local bufname = vim.api.nvim_buf_get_name(self.buf)
        local is_ipynb = bufname ~= '' and bufname:match('%.ipynb_interpreted$')
        local inside_pairs, _, _, start_cols = markdown_inside_ranges(self.buf)
        local had_included = false
        local set_ok = false
        if is_ipynb and #inside_pairs > 0 then
            local ir = ts_included_ranges_from_pairs(inside_pairs, start_cols)
            -- set_included_ranges signature (Neovim API): parser:set_included_ranges({ {sr, sc, er, ec}, ... })
            set_ok = pcall(function()
                parser:set_included_ranges(ir)
                had_included = true
            end)
        end
        -- reset buffer context
        local context = Context.new(self.buf, self.win, self.config, self.mode)
        if context then
            context.view:parse(parser, function()
                local marks = handlers.run(context, parser)
                callback(iter.list.map(marks, Extmark.new))
            end)
        else
            log.buf('debug', 'Skip', self.buf, 'in progress')
            callback(nil)
        end
        -- We deliberately do NOT restore included ranges; each call resets them.
        -- If needed in the future, we could parser:set_included_ranges(nil) here.
        if had_included and not set_ok then
            log.buf('warn', 'Warn', self.buf, 'failed to set included ranges')
        end
    else
        log.buf('error', 'Fail', self.buf, 'no treesitter parser found')
        callback(nil)
    end
end
---@private
function Updater:render()
    if self:changed() then
        self:parse(function(extmarks)
            if not extmarks then
                return
            end
            local bufname = vim.api.nvim_buf_get_name(self.buf)
            local is_ipynb = bufname ~= '' and bufname:match('%.ipynb_interpreted$')
            local inside_pairs, inside_ranges, maxline, _ = markdown_inside_ranges(self.buf)
            local filtered = extmarks
            if is_ipynb then
                if #inside_ranges > 0 then
                    filtered = {}
                    for _, em in ipairs(extmarks) do
                        for _, r in ipairs(inside_ranges) do
                            if em:overlaps(r) then
                                table.insert(filtered, em)
                                break
                            end
                        end
                    end
                else
                    filtered = {}
                end
            end
            local initial = self.decorator:initial()
            self:clear()
            self.decorator:set(filtered)
            if initial then
                compat.fix_lsp_window(self.buf, self.win, filtered)
                M.config.on.initial({ buf = self.buf, win = self.win })
            end
            self:display()
        end)
    else
        self:display()
    end
end
---@private
function Updater:display()
    local range = self:hidden()
    local extmarks = self.decorator:get()
    for _, extmark in ipairs(extmarks) do
        if extmark:get().conceal and extmark:overlaps(range) then
            extmark:hide(M.ns, self.buf)
        else
            extmark:show(M.ns, self.buf)
        end
    end
    M.config.on.render({ buf = self.buf, win = self.win })
end
---@private
---@return render.md.Range?
function Updater:hidden()
    -- anti-conceal is not enabled -> hide nothing
    -- in disabled mode -> hide nothing
    local config = self.config.anti_conceal
    if not config.enabled or env.mode.is(self.mode, config.disabled_modes) then
        return nil
    end
    -- row is not known -> buffer is not active -> hide nothing
    local row = env.row.get(self.buf, self.win)
    if not row then
        return nil
    end
    if env.mode.is(self.mode, { 'v', 'V', '\22' }) then
        local start = vim.fn.getpos('v')[2] - 1
        return Range.new(math.min(row, start), math.max(row, start))
    else
        return Range.new(row - config.above, row + config.below)
    end
end
---@private
M.updater = Updater
return M

-- lua/render-markdown/handler/markdown.lua
local Context = require('render-markdown.request.context')
local Marks = require('render-markdown.lib.marks')
local ts = require('render-markdown.core.ts')
local Str = require('render-markdown.lib.str')
local list = require('render-markdown.lib.list')
local api = vim.api

---@class render.md.handler.Markdown: render.md.Handler
local M = {}

-- Dedicated namespace and sign group for the fallback so we can wipe clean each render
local NS_FALLBACK = api.nvim_create_namespace('render_markdown_fallback_headings')
local SIGN_GROUP  = 'RenderMarkdownFallbackHeadings'

---@param ctx render.md.handler.Context
---@return render.md.Mark[]
function M.parse(ctx)
  -- Tree-sitter query (unchanged)
  local query = ts.parse('markdown', [[
      (fenced_code_block) @code
      [
        (thematic_break)
        (minus_metadata)
        (plus_metadata)
      ] @dash
      (document) @document
      [
        (atx_heading)
        (setext_heading)
      ] @heading
      (list_item) @list
      (section (paragraph) @paragraph)
      (block_quote) @quote
      (section) @section
      (pipe_table) @table
  ]])

  ---@type table<string, render.md.Render>
  local renders = {
    code = require('render-markdown.render.markdown.code'),
    dash = require('render-markdown.render.markdown.dash'),
    document = require('render-markdown.render.markdown.document'),
    heading = require('render-markdown.render.markdown.heading'),
    list = require('render-markdown.render.markdown.list'),
    paragraph = require('render-markdown.render.markdown.paragraph'),
    quote = require('render-markdown.render.markdown.quote'),
    section = require('render-markdown.render.markdown.section'),
    table = require('render-markdown.render.markdown.table'),
  }

  local context = Context.get(ctx.buf)
  local marks = Marks.new(context, false)

  -- Track rows TS already handled as headings
  local ts_heading_rows = {}

  -- Normal TS-driven rendering
  context.view:nodes(ctx.root, query, function(capture, node)
    if capture == 'heading' then
      for row = node.start_row, node.end_row - 1 do
        ts_heading_rows[row] = true
      end
    end
    local render = renders[capture]
    assert(render, ('unhandled markdown capture: %s'):format(capture))
    render:execute(context, marks, node)
  end)

  -- --- Fallback pass ---------------------------------------------------------
  -- Only render headings missed by TS when they directly follow an opening <markdown> line.
  -- Example:
  -- <markdown>
  -- # Hello
  local cfg = context.config.heading

  -- Always wipe our artifacts first so stale visuals never linger
  api.nvim_buf_clear_namespace(ctx.buf, NS_FALLBACK, 0, -1)
  pcall(vim.fn.sign_unplace, SIGN_GROUP, { buffer = ctx.buf })

  if cfg and cfg.enabled then
    local total = api.nvim_buf_line_count(ctx.buf)

    -- Only treat an exact "<markdown>" as opening marker for a markdown block
    local function is_markdown_open(line)
      return (line or ''):match('^%s*<markdown>%s*$') ~= nil
    end

    for row = 0, total - 1 do
      -- Skip rows already rendered by TS
      if not ts_heading_rows[row] then
        local cur = api.nvim_buf_get_lines(ctx.buf, row, row + 1, false)[1] or ''
        local lvl = Str.level(cur) -- 0 if not a hash heading, >0 otherwise
        if lvl > 0 then
          local prev = (row > 0) and (api.nvim_buf_get_lines(ctx.buf, row - 1, row, false)[1] or '') or ''
          if is_markdown_open(prev) then
            -- Derive visuals from heading config
            local inline_icon = (type(cfg.icons) == 'function')
              and cfg.icons({ level = lvl, sections = {} })
              or list.cycle(cfg.icons, lvl)
            local sign_icon = list.cycle(cfg.signs, lvl)
            local fg = list.clamp(cfg.foregrounds, lvl)
            local bg = list.clamp(cfg.backgrounds, lvl)

            -- 1) Background across the whole line (fallback namespace)
            if bg then
              api.nvim_buf_set_extmark(ctx.buf, NS_FALLBACK, row, 0, {
                end_row = row + 1,   -- full line
                hl_group = bg,
                hl_eol = true,
                priority = 90,
              })
            end

            -- Compute prefix range and start of visible text
            local prefix = cur:match('^%s*#+%s?') or ''
            local text_start = #prefix
            if text_start > #cur then text_start = #cur end

            -- 2) Conceal only the leading hashes (+ optional space) — same-line range
            if text_start > 0 then
              api.nvim_buf_set_extmark(ctx.buf, NS_FALLBACK, row, 0, {
                end_row = row,       -- SAME LINE range
                end_col = text_start, -- exclusive
                conceal = '',
                priority = 100,
              })
            end

            -- 3) Apply foreground highlight to the heading *text* (after the prefix)
            --    This makes the first heading colored like normal TS headings.
            if fg and text_start < #cur then
              api.nvim_buf_set_extmark(ctx.buf, NS_FALLBACK, row, text_start, {
                end_row = row,
                end_col = #cur,
                hl_group = fg,
                priority = 105,
              })
            end

            -- 4) Inline icon at BOL (fallback namespace)
            if inline_icon then
              local vt_hl = fg or bg or ''
              api.nvim_buf_set_extmark(ctx.buf, NS_FALLBACK, row, 0, {
                virt_text = { { inline_icon, vt_hl } },
                virt_text_pos = 'inline',
                priority = 110,
              })
            end

            -- 5) Optional sign in signcolumn (single group so we can clear it)
            if cfg.sign and sign_icon then
              local sign_hl_group = fg or ''
              local sign_name = ('RenderMarkdownHeading%d'):format(lvl)
              -- Define (safe if already exists)
              pcall(vim.fn.sign_define, sign_name, {
                text = sign_icon,
                texthl = sign_hl_group,
                linehl = bg, -- matches background if provided
              })
              -- Deterministic per-line id (1-based for Vim)
              local sign_id = row + 1
              vim.fn.sign_place(sign_id, SIGN_GROUP, sign_name, ctx.buf, {
                lnum = row + 1,
                priority = 100,
              })
            end
          end
        end
      end
    end
  end

  return marks:get()
end

return M


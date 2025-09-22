---@class render.md.Colors
local M = {}

M.colors = {}
M.cache = { combine = {}, bg_as_fg = {} }

local api = vim.api

-- wrap nvim_buf_set_extmark to strip colors but keep rendering
local orig_set_extmark = api.nvim_buf_set_extmark
api.nvim_buf_set_extmark = function(buf, ns, row, col, opts)
  if opts then
    opts.hl_group = nil      -- remove text coloring
    opts.linehl = nil        -- remove line highlights

    -- if virt_text exists, drop only its highlight part
    if opts.virt_text then
      local new_vt = {}
      for _, chunk in ipairs(opts.virt_text) do
        if type(chunk) == "table" then
          -- chunk = { text, hl }
          table.insert(new_vt, { chunk[1], nil }) -- keep text, drop color
        else
          table.insert(new_vt, chunk)
        end
      end
      opts.virt_text = new_vt
    end
  end
  return orig_set_extmark(buf, ns, row, col, opts)
end

-- disable sign highlights too
local orig_sign_define = vim.fn.sign_define
vim.fn.sign_define = function(name, opts)
  if opts then
    opts.texthl = nil
    opts.linehl = nil
    opts.numhl = nil
  end
  return orig_sign_define(name, opts)
end

function M.init() end
function M.reload() end
function M.combine(_, _, _) return '' end
function M.bg_as_fg(_, _) return '' end
function M.get_hl(_) return {} end

return M


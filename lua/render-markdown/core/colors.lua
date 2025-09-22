---@class render.md.Colors
local M = {}

M.colors = {}
M.cache = { combine = {}, bg_as_fg = {} }

local api = vim.api

-- helper: only strip colors in *.ipynb_interpreted buffers
local function is_ipynb_interpreted_buf(buf)
  if not buf or not api.nvim_buf_is_valid(buf) then return false end
  local ok, name = pcall(api.nvim_buf_get_name, buf)
  if not ok or not name then return false end
  return name:match("%.ipynb_interpreted$")
end

-- wrap nvim_buf_set_extmark to strip colors ONLY for *.ipynb_interpreted
do
  local orig_set_extmark = api.nvim_buf_set_extmark
  api.nvim_buf_set_extmark = function(buf, ns, row, col, opts)
    if opts and is_ipynb_interpreted_buf(buf) then
      -- remove text coloring
      opts.hl_group = nil
      opts.linehl = nil

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
end

-- IMPORTANT: do NOT monkey-patch sign_define globally.
-- If a previous version of this file replaced it, restore behavior here by
-- delegating to :sign define via nvim_call_function.
-- (This is safe to keep even after a restart.)
vim.fn.sign_define = function(name, opts)
  return api.nvim_call_function("sign_define", { name, opts or {} })
end

function M.init() end
function M.reload() end
function M.combine(_, _, _) return '' end
function M.bg_as_fg(_, _) return '' end
function M.get_hl(_) return {} end

return M


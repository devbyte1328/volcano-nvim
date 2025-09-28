# Volcano

Neovim plugin for running code interactively with the Jupyter kernel.

# This project is a *work in progress*.

## The following is for development.

### 🌐 Live Version
Create '~/.config/nvim/lua/plugins/volcano.lua':
```
return {
    "devbyte1328/volcano-nvim",
    version = "^1.0.0", -- use version <2.0.0 to avoid breaking changes
    build = ":UpdateRemotePlugins",
    dependencies = {
        {
            "nvim-treesitter/nvim-treesitter",
            build = ":TSUpdate",
        },
        "echasnovski/mini.nvim"
    },
    opts = {},
    config = function()
        -- Treesitter setup (moved here to make external treesitter.lua redundant)
        require("nvim-treesitter.configs").setup({
            ensure_installed = { "python", "markdown" },
            highlight = { enable = true },
            indent = { enable = true },
        })
        -- Python provider: local venv
        vim.g.python3_host_prog = vim.fn.expand("~/.config/nvim/venv/bin/python")
        -- Ensure Jupyter runtime dir
        local jupyter_runtime_dir = vim.fn.expand("~/.local/share/jupyter/runtime/")
        if vim.fn.isdirectory(jupyter_runtime_dir) == 0 then
            vim.fn.mkdir(jupyter_runtime_dir, "p")
        end
        -- Molten config
        vim.g.molten_output_win_max_height = 12
        -- Auto-run :VolcanoInit on .ipynb open
        vim.api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function()
                vim.cmd("VolcanoInit")
            end,
        })
        -- Auto-run :SaveIPYNB after saving interpreted notebook files
        vim.api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb_interpreted",
            callback = function()
                vim.cmd("SaveIPYNB")
            end,
        })
        -------------------------------------------------------------------------
        -- IMPORTANT: do NOT register a custom 'ipynb_interpreted' filetype
        -- Anywhere else in your config. This block *forces* markdown for that
        -- extension, even if a plugin or modeline tries to change it later.
        -------------------------------------------------------------------------
        local grp = vim.api.nvim_create_augroup("ForceIpynbInterpretedAsMarkdown", { clear = true })
        local function force_markdown(buf)
            if not buf or not vim.api.nvim_buf_is_valid(buf) then return end
            local name = vim.api.nvim_buf_get_name(buf)
            if name:match("%.ipynb_interpreted$") and vim.bo[buf].filetype ~= "markdown" then
                -- setfiletype avoids re-triggering detection loops
                vim.api.nvim_buf_call(buf, function()
                    vim.cmd("setfiletype markdown")
                end)
            end
        end
        -- On open/create of those files
        vim.api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
                -- Defer once more to beat late plugins/modelines
                vim.defer_fn(function() force_markdown(args.buf) end, 0)
            end,
        })
        -- If any plugin flips filetype later
        vim.api.nvim_create_autocmd("FileType", {
            group = grp,
            pattern = "*",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })
        -- If the 'filetype' option changes for any reason
        vim.api.nvim_create_autocmd("OptionSet", {
            group = grp,
            pattern = "filetype",
            callback = function()
                local buf = vim.api.nvim_get_current_buf()
                force_markdown(buf)
            end,
        })
        -- When entering the window (catches some late-setters)
        vim.api.nvim_create_autocmd("BufEnter", {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })
        -- syntax color for interpreted file
        -- ends with .ipynb_interpreted (still uses markdown ft)
        vim.api.nvim_create_autocmd("FileType", {
            group = vim.api.nvim_create_augroup("IpynbInterpretedSyntax", { clear = true }),
            pattern = "markdown",
            callback = function(args)
                local bufname = vim.api.nvim_buf_get_name(args.buf)
                if not bufname:match("%.ipynb_interpreted$") then return end
                vim.schedule(function()
                    vim.cmd([[
                        " Match tags
                        syntax match IPYNBCellTag /^<cell>$/ containedin=ALL
                        syntax match IPYNBCellTag /^<\/cell>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<output>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<\/output>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<markdown>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<\/markdown>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<raw>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<\/raw>$/ containedin=ALL
                        " Include Python syntax in code cells
                        syntax include @Python syntax/python.vim
                        " Define block regions
                        syntax region IPYNBPython start=/^<cell>$/ end=/^<\/cell>$/ contains=@Python keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend
                        " Text content matches
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBRawText /.*/ contained
                        " Status markers inside output
                        syntax match IPYNBEvalRunning /\v\[\*\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalDone /\v\[Done\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalError /\v\[Error\]/ containedin=IPYNBOutputText
                        " Tag highlighting (dark gray, italic)
                        highlight IPYNBCellTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBMarkdownTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBRawTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        " Content highlighting
                        highlight IPYNBOutputText gui=NONE cterm=NONE
                        highlight IPYNBRawText guifg=#dddddd ctermfg=252
                        " Output status highlighting
                        highlight IPYNBEvalRunning guifg=orange ctermfg=208
                        highlight IPYNBEvalDone guifg=green ctermfg=34
                        highlight IPYNBEvalError guifg=red ctermfg=196
                    ]])
                end)
            end,
        })
    end,
}
```

Create venv _somwhere?_(Figure out perm solution for venv) '~/.config/nvim/':
```
python -m venv venv
```

```
source venv/bin/activate
```

```
pip install pynvim jupyter_client jupyter 
```

Add these lines to setup the keymaps '~/.config/nvim/init.lua':
```
vim.api.nvim_create_autocmd("FileType", {
  pattern = "ipynb_interpreted",
  callback = function()
    vim.keymap.set("n", "<Space>", "<Nop>") -- Disable <Space> default behavior
    vim.keymap.set("n", "<CR>", ":VolcanoEvaluate<CR>", { desc = "Run current cell" })
    vim.keymap.set("n", "<leader><CR>", ":VolcanoEvaluateJump<CR>", { desc = "Run current cell" })
    vim.keymap.set("n", "<leader>k<CR>", ":VolcanoEvaluateAbove<CR>", { desc = "Run cells above" })
    vim.keymap.set("n", "<leader>j<CR>", ":VolcanoEvaluateBelow<CR>", { desc = "Run cells below" })
    vim.keymap.set("n", "<leader><leader><CR>", ":VolcanoEvaluateAll<CR>", { desc = "Run all cells" })
    vim.keymap.set("n", "<leader>do<CR>", ":VolcanoDeleteOutput<CR>", { desc = "Delete all outputs" })
    vim.keymap.set("n", "<leader>dok<CR>", ":VolcanoDeleteOutputAbove<CR>", { desc = "Delete outputs above" })
    vim.keymap.set("n", "<leader>doj<CR>", ":VolcanoDeleteOutputBelow<CR>", { desc = "Delete outputs below" })
    vim.keymap.set("n", "<leader>nck<CR>", ":VolcanoCreateCellUpward<CR>", { desc = "Create new cell above" })
    vim.keymap.set("n", "<leader>ncj<CR>", ":VolcanoCreateCellDownward<CR>", { desc = "Create new cell below" })
    vim.keymap.set("n", "<leader>.", ":VolcanoSwitchCellTypeForward<CR>", { desc = "Next cell type" })
    vim.keymap.set("n", "<leader>,", ":VolcanoSwitchCellTypeBackward<CR>", { desc = "Previous cell type" })
  end,
})
```


### 💻 Local Version (For testing)

Create folder to hold local plugins:
```
mkdir -p ~/.config/nvim/lua/local_plugins
```

Clone, copy to nvim folder, and clean up repository:
```
git clone https://github.com/devbyte1328/volcano-nvim.git
```

```
cp -r volcano-nvim ~/.config/nvim/lua/local_plugins
```

```
sudo rm -r volcano-nvim
```

Create '~/.config/nvim/lua/plugins/volcano.lua':
```
return {
    dir = vim.fn.stdpath("config") .. "/lua/local_plugins/volcano-nvim",
    name = "volcano-nvim",
    dependencies = {
        {
            "nvim-treesitter/nvim-treesitter",
            build = ":TSUpdate",
        },
        "echasnovski/mini.nvim"
    },
    opts = {},
    config = function()
        -- Treesitter setup (moved here to make external treesitter.lua redundant)
        require("nvim-treesitter.configs").setup({
            ensure_installed = { "python", "markdown" },
            highlight = { enable = true },
            indent = { enable = true },
        })

        -- Python provider: local venv
        vim.g.python3_host_prog = vim.fn.expand("~/.config/nvim/venv/bin/python")
        -- Ensure Jupyter runtime dir
        local jupyter_runtime_dir = vim.fn.expand("~/.local/share/jupyter/runtime/")
        if vim.fn.isdirectory(jupyter_runtime_dir) == 0 then
            vim.fn.mkdir(jupyter_runtime_dir, "p")
        end
        -- Molten config
        vim.g.molten_output_win_max_height = 12
        -- Auto-run :VolcanoInit on .ipynb open
        vim.api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function()
                vim.cmd("VolcanoInit")
            end,
        })
        -- Auto-run :SaveIPYNB after saving interpreted notebook files
        vim.api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb_interpreted",
            callback = function()
                vim.cmd("SaveIPYNB")
            end,
        })
        -------------------------------------------------------------------------
        -- IMPORTANT: do NOT register a custom 'ipynb_interpreted' filetype
        -- Anywhere else in your config. This block *forces* markdown for that
        -- extension, even if a plugin or modeline tries to change it later.
        -------------------------------------------------------------------------
        local grp = vim.api.nvim_create_augroup("ForceIpynbInterpretedAsMarkdown", { clear = true })
        local function force_markdown(buf)
            if not buf or not vim.api.nvim_buf_is_valid(buf) then return end
            local name = vim.api.nvim_buf_get_name(buf)
            if name:match("%.ipynb_interpreted$") and vim.bo[buf].filetype ~= "markdown" then
                -- setfiletype avoids re-triggering detection loops
                vim.api.nvim_buf_call(buf, function()
                    vim.cmd("setfiletype markdown")
                end)
            end
        end
        -- On open/create of those files
        vim.api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
                -- Defer once more to beat late plugins/modelines
                vim.defer_fn(function() force_markdown(args.buf) end, 0)
            end,
        })
        -- If any plugin flips filetype later
        vim.api.nvim_create_autocmd("FileType", {
            group = grp,
            pattern = "*",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })
        -- If the 'filetype' option changes for any reason
        vim.api.nvim_create_autocmd("OptionSet", {
            group = grp,
            pattern = "filetype",
            callback = function()
                local buf = vim.api.nvim_get_current_buf()
                force_markdown(buf)
            end,
        })
        -- When entering the window (catches some late-setters)
        vim.api.nvim_create_autocmd("BufEnter", {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })
        -- syntax color for interpreted file
        -- ends with .ipynb_interpreted (still uses markdown ft)
        vim.api.nvim_create_autocmd("FileType", {
            group = vim.api.nvim_create_augroup("IpynbInterpretedSyntax", { clear = true }),
            pattern = "markdown",
            callback = function(args)
                local bufname = vim.api.nvim_buf_get_name(args.buf)
                if not bufname:match("%.ipynb_interpreted$") then return end
                vim.schedule(function()
                    vim.cmd([[
                        " Match tags
                        syntax match IPYNBCellTag /^<cell>$/ containedin=ALL
                        syntax match IPYNBCellTag /^<\/cell>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<output>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<\/output>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<markdown>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<\/markdown>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<raw>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<\/raw>$/ containedin=ALL
                        " Include Python syntax in code cells
                        syntax include @Python syntax/python.vim
                        " Define block regions
                        syntax region IPYNBPython start=/^<cell>$/ end=/^<\/cell>$/ contains=@Python keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend
                        " Text content matches
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBRawText /.*/ contained
                        " Status markers inside output
                        syntax match IPYNBEvalRunning /\v\[\*\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalDone /\v\[Done\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalError /\v\[Error\]/ containedin=IPYNBOutputText
                        " Tag highlighting (dark gray, italic)
                        highlight IPYNBCellTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBMarkdownTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBRawTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        " Content highlighting
                        highlight IPYNBOutputText gui=NONE cterm=NONE
                        highlight IPYNBRawText guifg=#dddddd ctermfg=252
                        " Output status highlighting
                        highlight IPYNBEvalRunning guifg=orange ctermfg=208
                        highlight IPYNBEvalDone guifg=green ctermfg=34
                        highlight IPYNBEvalError guifg=red ctermfg=196
                    ]])
                end)
            end,
        })
    end,
}
```

Create venv _somwhere?_(Figure out perm solution for venv) '~/.config/nvim/':
```
python -m venv venv
```

```
source venv/bin/activate
```

```
pip install pynvim jupyter_client jupyter 
```

Add these lines to setup the keymaps '~/.config/nvim/init.lua':
```
vim.api.nvim_create_autocmd("FileType", {
  pattern = "ipynb_interpreted",
  callback = function()
    vim.keymap.set("n", "<Space>", "<Nop>") -- Disable <Space> default behavior
    vim.keymap.set("n", "<CR>", ":VolcanoEvaluate<CR>", { desc = "Run current cell" })
    vim.keymap.set("n", "<leader><CR>", ":VolcanoEvaluateJump<CR>", { desc = "Run current cell" })
    vim.keymap.set("n", "<leader>k<CR>", ":VolcanoEvaluateAbove<CR>", { desc = "Run cells above" })
    vim.keymap.set("n", "<leader>j<CR>", ":VolcanoEvaluateBelow<CR>", { desc = "Run cells below" })
    vim.keymap.set("n", "<leader><leader><CR>", ":VolcanoEvaluateAll<CR>", { desc = "Run all cells" })
    vim.keymap.set("n", "<leader>do<CR>", ":VolcanoDeleteOutput<CR>", { desc = "Delete all outputs" })
    vim.keymap.set("n", "<leader>dok<CR>", ":VolcanoDeleteOutputAbove<CR>", { desc = "Delete outputs above" })
    vim.keymap.set("n", "<leader>doj<CR>", ":VolcanoDeleteOutputBelow<CR>", { desc = "Delete outputs below" })
    vim.keymap.set("n", "<leader>nck<CR>", ":VolcanoCreateCellUpward<CR>", { desc = "Create new cell above" })
    vim.keymap.set("n", "<leader>ncj<CR>", ":VolcanoCreateCellDownward<CR>", { desc = "Create new cell below" })
    vim.keymap.set("n", "<leader>.", ":VolcanoSwitchCellTypeForward<CR>", { desc = "Next cell type" })
    vim.keymap.set("n", "<leader>,", ":VolcanoSwitchCellTypeBackward<CR>", { desc = "Previous cell type" })
  end,
})
```

## Credits

- [Molten](https://github.com/benlubas/molten-nvim): Original fork, GPL-licensed, basis for project.
- [render-markdown.nvim](https://github.com/MeanderingProgrammer/render-markdown.nvim): Core function copied and modified for specific use case.
- [nvim-treesitter](https://github.com/nvim-treesitter/nvim-treesitter): Used for syntax coloring

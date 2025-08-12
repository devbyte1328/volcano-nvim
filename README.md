# Volcano

Volcano is a fork of [Molten](https://github.com/benlubas/molten-nvim), a plugin for running code
interactively with the jupyter kernel. Volcano is a work in progress.

## Work in Progress, the following is for development.

### 🌐 Live Version
Create '~/.config/nvim/lua/plugins/volcano.lua':
```
return {
    "devbyte1328/volcano-nvim",
    version = "^1.0.0", -- use version <2.0.0 to avoid breaking changes
    build = ":UpdateRemotePlugins",
    config = function()
        -- Set up Python provider to use local venv
        vim.g.python3_host_prog = vim.fn.expand("~/.config/nvim/venv/bin/python")

        -- Ensure Jupyter runtime directory exists
        local jupyter_runtime_dir = vim.fn.expand("~/.local/share/jupyter/runtime/")
        if vim.fn.isdirectory(jupyter_runtime_dir) == 0 then
            vim.fn.mkdir(jupyter_runtime_dir, "p")
        end

        -- Molten config
        vim.g.molten_output_win_max_height = 12

        -- Auto-run :VolcanoInit if opening a .ipynb file
        vim.api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function()
                vim.cmd("VolcanoInit")
            end,
        })

        -- Auto-run :SaveIPYNB after saving interpreted notebook files
        vim.api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb.interpreted",
            callback = function()
                vim.cmd("SaveIPYNB")
            end,
        })

        -- Register custom filetype
        vim.filetype.add({
            extension = {
                ["ipynb.interpreted"] = "ipynb_interpreted",
            },
        })

        -- Manually enforce filetype
        vim.api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            pattern = "*.ipynb.interpreted",
            command = "set filetype=ipynb_interpreted",
        })

        -- Syntax highlighting for ipynb_interpreted
        vim.api.nvim_create_autocmd("FileType", {
            pattern = "ipynb_interpreted",
            callback = function()
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
                        syntax region IPYNBMarkdownContent start=/^<markdown>$/ end=/^<\/markdown>$/ contains=IPYNBMarkdownText keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend

                        " Text content matches
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBMarkdownText /.*/ contained
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
                        highlight IPYNBMarkdownText guifg=#dddddd ctermfg=252
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
    vim.keymap.set("n", "<Space>", "<Nop>")
    vim.keymap.set("n", "<CR>", function() vim.cmd("VolcanoEvaluate") end)
    vim.keymap.set("n", "<leader><leader><CR>", function() vim.cmd("VolcanoEvaluateAll") end)
    vim.keymap.set("n", "<leader><CR>", function() vim.cmd("VolcanoEvaluateJump") end)
    vim.keymap.set("n", "<leader>k<CR>", function() vim.cmd("VolcanoEvaluateAbove") end)
    vim.keymap.set("n", "<leader>j<CR>", function() vim.cmd("VolcanoEvaluateBelow") end)
    vim.keymap.set("n", "<leader>c", function() vim.cmd("VolcanoDeleteOutput") end)
    vim.keymap.set("n", "<leader>kc", function() vim.cmd("VolcanoDeleteOutputAbove") end)
    vim.keymap.set("n", "<leader>jc", function() vim.cmd("VolcanoDeleteOutputBelow") end)
    vim.keymap.set("n", "<leader>.", function() vim.cmd("VolcanoSwitchCellTypeForward") end)
    vim.keymap.set("n", "<leader>,", function() vim.cmd("VolcanoSwitchCellTypeBackward") end)
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
    build = ":UpdateRemotePlugins",
    config = function()
        -- Set up Python provider to use local venv
        vim.g.python3_host_prog = vim.fn.expand("~/.config/nvim/venv/bin/python")

        -- Ensure Jupyter runtime directory exists
        local jupyter_runtime_dir = vim.fn.expand("~/.local/share/jupyter/runtime/")
        if vim.fn.isdirectory(jupyter_runtime_dir) == 0 then
            vim.fn.mkdir(jupyter_runtime_dir, "p")
        end

        -- Molten config
        vim.g.molten_output_win_max_height = 12

        -- Auto-run :VolcanoInit if opening a .ipynb file
        vim.api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function()
                vim.cmd("VolcanoInit")
            end,
        })

        -- Auto-run :SaveIPYNB after saving interpreted notebook files
        vim.api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb.interpreted",
            callback = function()
                vim.cmd("SaveIPYNB")
            end,
        })

        -- Register custom filetype
        vim.filetype.add({
            extension = {
                ["ipynb.interpreted"] = "ipynb_interpreted",
            },
        })

        -- Manually enforce filetype
        vim.api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            pattern = "*.ipynb.interpreted",
            command = "set filetype=ipynb_interpreted",
        })

        -- Syntax highlighting for ipynb_interpreted
        vim.api.nvim_create_autocmd("FileType", {
            pattern = "ipynb_interpreted",
            callback = function()
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
                        syntax region IPYNBMarkdownContent start=/^<markdown>$/ end=/^<\/markdown>$/ contains=IPYNBMarkdownText keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend

                        " Text content matches
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBMarkdownText /.*/ contained
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
                        highlight IPYNBMarkdownText guifg=#dddddd ctermfg=252
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
    vim.keymap.set("n", "<Space>", "<Nop>")
    vim.keymap.set("n", "<CR>", function() vim.cmd("VolcanoEvaluate") end)
    vim.keymap.set("n", "<leader><leader><CR>", function() vim.cmd("VolcanoEvaluateAll") end)
    vim.keymap.set("n", "<leader><CR>", function() vim.cmd("VolcanoEvaluateJump") end)
    vim.keymap.set("n", "<leader>k<CR>", function() vim.cmd("VolcanoEvaluateAbove") end)
    vim.keymap.set("n", "<leader>j<CR>", function() vim.cmd("VolcanoEvaluateBelow") end)
    vim.keymap.set("n", "<leader>c", function() vim.cmd("VolcanoDeleteOutput") end)
    vim.keymap.set("n", "<leader>kc", function() vim.cmd("VolcanoDeleteOutputAbove") end)
    vim.keymap.set("n", "<leader>jc", function() vim.cmd("VolcanoDeleteOutputBelow") end)
    vim.keymap.set("n", "<leader>.", function() vim.cmd("VolcanoSwitchCellTypeForward") end)
    vim.keymap.set("n", "<leader>,", function() vim.cmd("VolcanoSwitchCellTypeBackward") end)
  end,
})
```

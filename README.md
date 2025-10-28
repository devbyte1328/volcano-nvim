# Volcano

Neovim plugin for running code interactively with the Jupyter kernel.

# This project is a *work in progress*.

## The following is for development.

### 🌐 Live Version
Create '~/.config/nvim/lua/plugins/volcano.lua':
```
return {
    "devbyte1328/volcano-nvim",
    version = "^1.0.0",
    build = ":UpdateRemotePlugins",
    dependencies = {
        {
            "nvim-treesitter/nvim-treesitter",
            build = ":TSUpdate",
        },
        "echasnovski/mini.nvim",
    },
    opts = {},
    config = function()
        local fn = vim.fn
        local cmd = vim.cmd
        local api = vim.api

	---------------------------------------------------------------------------
	-- Helper: check if a Python package is installed in a venv
	---------------------------------------------------------------------------
	local function has_python_packages(venv_path)
	    local python_bin = venv_path .. "/bin/python"
	    if fn.executable(python_bin) == 0 then
		return false
	    end
	    local check_cmd = string.format(
		"%s -c 'import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(p) for p in [\"pynvim\",\"jupyter_client\",\"jupyter\"]) else 1)'",
		python_bin
	    )
	    local result = os.execute(check_cmd)
	    return result == true or result == 0
	end

	---------------------------------------------------------------------------
	-- Helper: create a venv and install/copy dependencies
	---------------------------------------------------------------------------
	local function ensure_venv(venv_path)
	    local default_venv = fn.expand("~/.config/nvim/venv")

	    -- Create new venv if missing
	    if fn.isdirectory(venv_path) == 0 then
		fn.mkdir(venv_path, "p")
		os.execute(string.format("python -m venv %s", venv_path))
	    end

	    -- Ensure default venv exists and has required packages
	    if fn.isdirectory(default_venv) == 0 or not has_python_packages(default_venv) then
		fn.mkdir(default_venv, "p")
		os.execute(string.format("python -m venv %s", default_venv))
		os.execute(string.format("%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter", default_venv))
	    end

	    -- Copy site-packages and binaries from default venv
	    if not has_python_packages(venv_path) then
		local src_site = fn.system(string.format(
		    '%s/bin/python -c "import site; print(site.getsitepackages()[0])"',
		    default_venv
		)):gsub("%s+$", "")

		local dst_site = fn.system(string.format(
		    '%s/bin/python -c "import site; print(site.getsitepackages()[0])"',
		    venv_path
		)):gsub("%s+$", "")

		if fn.isdirectory(src_site) ~= 0 and fn.isdirectory(dst_site) ~= 0 then
		    os.execute(string.format("cp -r %s/* %s/ >/dev/null 2>&1", src_site, dst_site))
		end

		-- Copy Python and pip binaries
		os.execute(string.format("cp %s/bin/python %s/bin/ 2>/dev/null || true", default_venv, venv_path))
		os.execute(string.format("cp %s/bin/pip %s/bin/ 2>/dev/null || true", default_venv, venv_path))

		-- Final fallback to pip install if still incomplete
		if not has_python_packages(venv_path) then
		    os.execute(string.format(
		        "%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter",
		        venv_path
		    ))
		end
	    end

	    -- Final check, fallback to pip install if copy failed
	    if not has_python_packages(venv_path) then
		os.execute(string.format("%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter", venv_path))
	    end
	end

        -------------------------------------------------------------------------
        -- 1. Default venv check (~/.config/nvim/venv)
        -------------------------------------------------------------------------
        local default_venv = fn.expand("~/.config/nvim/venv")
        ensure_venv(default_venv)
        vim.g.python3_host_prog = default_venv .. "/bin/python"

        -------------------------------------------------------------------------
        -- Treesitter setup
        -------------------------------------------------------------------------
        require("nvim-treesitter.configs").setup({
            ensure_installed = { "python", "markdown" },
            highlight = { enable = true },
            indent = { enable = true },
        })

        -------------------------------------------------------------------------
        -- Ensure Jupyter runtime dir
        -------------------------------------------------------------------------
        local jupyter_runtime_dir = fn.expand("~/.local/share/jupyter/runtime/")
        if fn.isdirectory(jupyter_runtime_dir) == 0 then
            fn.mkdir(jupyter_runtime_dir, "p")
        end
        vim.g.molten_output_win_max_height = 12

        -------------------------------------------------------------------------
        -- 2. Per-notebook venv detection and switching
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function(args)
                local file_dir = fn.fnamemodify(args.file, ":p:h")
                local local_venv = file_dir .. "/venv"
                if fn.isdirectory(local_venv) ~= 0 then
                    if has_python_packages(local_venv) then
                        vim.g.python3_host_prog = local_venv .. "/bin/python"
                    else
                        ensure_venv(local_venv)
                        vim.g.python3_host_prog = local_venv .. "/bin/python"
                    end
                else
                    -- fallback to default
                    vim.g.python3_host_prog = default_venv .. "/bin/python"
                end
                cmd("VolcanoInit")
            end,
        })

        -------------------------------------------------------------------------
        -- Auto-save .ipynb_interpreted -> .ipynb
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb_interpreted",
            callback = function()
                cmd("SaveIPYNB")
            end,
        })

        -------------------------------------------------------------------------
        -- Force .ipynb_interpreted as markdown (prevents type conflicts)
        -------------------------------------------------------------------------
        local grp = api.nvim_create_augroup("ForceIpynbInterpretedAsMarkdown", { clear = true })
        local function force_markdown(buf)
            if not buf or not api.nvim_buf_is_valid(buf) then return end
            local name = api.nvim_buf_get_name(buf)
            if name:match("%.ipynb_interpreted$") and vim.bo[buf].filetype ~= "markdown" then
                api.nvim_buf_call(buf, function()
                    cmd("setfiletype markdown")
                end)
            end
        end

        api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
                vim.defer_fn(function() force_markdown(args.buf) end, 0)
            end,
        })

        api.nvim_create_autocmd("FileType", {
            group = grp,
            pattern = "*",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })

        api.nvim_create_autocmd("OptionSet", {
            group = grp,
            pattern = "filetype",
            callback = function()
                local buf = api.nvim_get_current_buf()
                force_markdown(buf)
            end,
        })

        api.nvim_create_autocmd("BufEnter", {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })

        -------------------------------------------------------------------------
        -- Syntax highlight for interpreted notebooks
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("FileType", {
            group = api.nvim_create_augroup("IpynbInterpretedSyntax", { clear = true }),
            pattern = "markdown",
            callback = function(args)
                local bufname = api.nvim_buf_get_name(args.buf)
                if not bufname:match("%.ipynb_interpreted$") then return end
                vim.schedule(function()
                    cmd([[
                        syntax match IPYNBCellTag /^<cell>$/ containedin=ALL
                        syntax match IPYNBCellTag /^<\/cell>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<output>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<\/output>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<markdown>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<\/markdown>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<raw>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<\/raw>$/ containedin=ALL

                        syntax include @Python syntax/python.vim
                        syntax region IPYNBPython start=/^<cell>$/ end=/^<\/cell>$/ contains=@Python keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBRawText /.*/ contained

                        syntax match IPYNBEvalRunning /\v\[\*\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalDone /\v\[Done\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalError /\v\[Error\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Restarted /\v\[Kernel_Restarted\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Interrupted /\v\[Kernel_Interrupted\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Stopped /\v\[Kernel_Stopped\]/ containedin=IPYNBOutputText

                        highlight IPYNBCellTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBMarkdownTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBRawTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputText gui=NONE cterm=NONE
                        highlight IPYNBRawText guifg=#dddddd ctermfg=252
                        highlight IPYNBEvalRunning guifg=orange ctermfg=208
                        highlight IPYNBEvalDone guifg=green ctermfg=34
                        highlight IPYNBEvalError guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Restarted guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Interrupted guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Stopped guifg=red ctermfg=196
                    ]])
                end)
            end,
        })
    end,
}
```

Add these lines to setup the keymaps '~/.config/nvim/init.lua':
```
vim.api.nvim_create_autocmd("FileType", {
  pattern = "ipynb_interpreted",
  callback = function()
	vim.keymap.set("n", "<Space>", "<Nop>", { desc = "Disable Space default behavior" })
	vim.keymap.set("n", "<CR>", function() vim.cmd("VolcanoEvaluate") end, { desc = "Run current cell" })
	vim.keymap.set("n", "<leader><CR>", function() vim.cmd("VolcanoEvaluateJump") end, { desc = "Run current cell and jump" })
	vim.keymap.set("n", "<leader>k<CR>", function() vim.cmd("VolcanoEvaluateAbove") end, { desc = "Run cells above" })
	vim.keymap.set("n", "<leader>j<CR>", function() vim.cmd("VolcanoEvaluateBelow") end, { desc = "Run cells below" })
	vim.keymap.set("n", "<leader><leader><CR>", function() vim.cmd("VolcanoEvaluateAll") end, { desc = "Run all cells" })
	vim.keymap.set("n", "<leader>do<CR>", function() vim.cmd("VolcanoDeleteOutput") end, { desc = "Delete output" })
	vim.keymap.set("n", "<leader><leader>do<CR>", function() vim.cmd("VolcanoDeleteAllOutputs") end, { desc = "Delete all outputs" })
	vim.keymap.set("n", "<leader>dok<CR>", function() vim.cmd("VolcanoDeleteOutputsAbove") end, { desc = "Delete outputs above" })
	vim.keymap.set("n", "<leader>doj<CR>", function() vim.cmd("VolcanoDeleteOutputsBelow") end, { desc = "Delete outputs below" })
	vim.keymap.set("n", "<leader>nck<CR>", function() vim.cmd("VolcanoCreateCellUpward") end, { desc = "Create new cell above" })
	vim.keymap.set("n", "<leader>ncj<CR>", function() vim.cmd("VolcanoCreateCellDownward") end, { desc = "Create new cell below" })
	vim.keymap.set("n", "<leader>.", function() vim.cmd("VolcanoSwitchCellTypeForward") end, { desc = "Next cell type" })
	vim.keymap.set("n", "<leader>,", function() vim.cmd("VolcanoSwitchCellTypeBackward") end, { desc = "Previous cell type" })
	vim.keymap.set("n", "<leader>mck<CR>", function() vim.cmd("VolcanoMoveCellUpward") end, { desc = "Move cell up" })
	vim.keymap.set("n", "<leader>mcj<CR>", function() vim.cmd("VolcanoMoveCellDownward") end, { desc = "Move cell down" })
	vim.keymap.set("n", "<leader>dc<CR>", function() vim.cmd("VolcanoDeleteCell") end, { desc = "Delete cell" })
	vim.keymap.set("n", "<leader>cc<CR>", function() vim.cmd("VolcanoCopyCell") end, { desc = "Copy cell" })
	vim.keymap.set("n", "<leader>pc<CR>", function() vim.cmd("VolcanoPasteCell") end, { desc = "Paste cell" })
	vim.keymap.set("n", "<leader>ik<CR>", function() vim.cmd("VolcanoInterrupt") end, { desc = "Interrupt Kernel" })
	vim.keymap.set("n", "<leader>rk<CR>", function() vim.cmd("VolcanoRestart") end, { desc = "Restart Kernel" })
	vim.keymap.set("n", "<leader>rkdo<CR>", function() vim.cmd("VolcanoRestartAndDeleteAllOutput") end, { desc = "Restart Kernel and Clear Output" })
	vim.keymap.set("n", "<leader><leader>rk<CR>", function() vim.cmd("VolcanoRestartAndEvaluateAll") end, { desc = "Restart Kernel and Run All Cells" })
	vim.keymap.set("n", "<leader>rkc<CR>", function() vim.cmd("VolcanoRestartAndEvaluateUpToCursor") end, { desc = "Restart Kernel and Run Up To Cursor" })
	vim.keymap.set("n", "<leader>vi<CR>", function() vim.cmd("VolcanoInfo") end, { desc = "Show information about kernels" })
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
        "echasnovski/mini.nvim",
    },
    opts = {},
    config = function()
        local fn = vim.fn
        local cmd = vim.cmd
        local api = vim.api

	---------------------------------------------------------------------------
	-- Helper: check if a Python package is installed in a venv
	---------------------------------------------------------------------------
	local function has_python_packages(venv_path)
	    local python_bin = venv_path .. "/bin/python"
	    if fn.executable(python_bin) == 0 then
		return false
	    end
	    local check_cmd = string.format(
		"%s -c 'import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(p) for p in [\"pynvim\",\"jupyter_client\",\"jupyter\"]) else 1)'",
		python_bin
	    )
	    local result = os.execute(check_cmd)
	    return result == true or result == 0
	end

	---------------------------------------------------------------------------
	-- Helper: create a venv and install/copy dependencies
	---------------------------------------------------------------------------
	local function ensure_venv(venv_path)
	    local default_venv = fn.expand("~/.config/nvim/venv")

	    -- Create new venv if missing
	    if fn.isdirectory(venv_path) == 0 then
		fn.mkdir(venv_path, "p")
		os.execute(string.format("python -m venv %s", venv_path))
	    end

	    -- Ensure default venv exists and has required packages
	    if fn.isdirectory(default_venv) == 0 or not has_python_packages(default_venv) then
		fn.mkdir(default_venv, "p")
		os.execute(string.format("python -m venv %s", default_venv))
		os.execute(string.format("%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter", default_venv))
	    end

	    -- Copy site-packages and binaries from default venv
	    if not has_python_packages(venv_path) then
		local src_site = fn.system(string.format(
		    '%s/bin/python -c "import site; print(site.getsitepackages()[0])"',
		    default_venv
		)):gsub("%s+$", "")

		local dst_site = fn.system(string.format(
		    '%s/bin/python -c "import site; print(site.getsitepackages()[0])"',
		    venv_path
		)):gsub("%s+$", "")

		if fn.isdirectory(src_site) ~= 0 and fn.isdirectory(dst_site) ~= 0 then
		    os.execute(string.format("cp -r %s/* %s/ >/dev/null 2>&1", src_site, dst_site))
		end

		-- Copy Python and pip binaries
		os.execute(string.format("cp %s/bin/python %s/bin/ 2>/dev/null || true", default_venv, venv_path))
		os.execute(string.format("cp %s/bin/pip %s/bin/ 2>/dev/null || true", default_venv, venv_path))

		-- Final fallback to pip install if still incomplete
		if not has_python_packages(venv_path) then
		    os.execute(string.format(
		        "%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter",
		        venv_path
		    ))
		end
	    end

	    -- Final check, fallback to pip install if copy failed
	    if not has_python_packages(venv_path) then
		os.execute(string.format("%s/bin/python -m pip install -U pip pynvim jupyter_client jupyter", venv_path))
	    end
	end

        -------------------------------------------------------------------------
        -- 1. Default venv check (~/.config/nvim/venv)
        -------------------------------------------------------------------------
        local default_venv = fn.expand("~/.config/nvim/venv")
        ensure_venv(default_venv)
        vim.g.python3_host_prog = default_venv .. "/bin/python"

        -------------------------------------------------------------------------
        -- Treesitter setup
        -------------------------------------------------------------------------
        require("nvim-treesitter.configs").setup({
            ensure_installed = { "python", "markdown" },
            highlight = { enable = true },
            indent = { enable = true },
        })

        -------------------------------------------------------------------------
        -- Ensure Jupyter runtime dir
        -------------------------------------------------------------------------
        local jupyter_runtime_dir = fn.expand("~/.local/share/jupyter/runtime/")
        if fn.isdirectory(jupyter_runtime_dir) == 0 then
            fn.mkdir(jupyter_runtime_dir, "p")
        end
        vim.g.molten_output_win_max_height = 12

        -------------------------------------------------------------------------
        -- 2. Per-notebook venv detection and switching
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("BufReadPost", {
            pattern = "*.ipynb",
            callback = function(args)
                local file_dir = fn.fnamemodify(args.file, ":p:h")
                local local_venv = file_dir .. "/venv"
                if fn.isdirectory(local_venv) ~= 0 then
                    if has_python_packages(local_venv) then
                        vim.g.python3_host_prog = local_venv .. "/bin/python"
                    else
                        ensure_venv(local_venv)
                        vim.g.python3_host_prog = local_venv .. "/bin/python"
                    end
                else
                    -- fallback to default
                    vim.g.python3_host_prog = default_venv .. "/bin/python"
                end
                cmd("VolcanoInit")
            end,
        })

        -------------------------------------------------------------------------
        -- Auto-save .ipynb_interpreted -> .ipynb
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("BufWritePost", {
            pattern = "*.ipynb_interpreted",
            callback = function()
                cmd("SaveIPYNB")
            end,
        })

        -------------------------------------------------------------------------
        -- Force .ipynb_interpreted as markdown (prevents type conflicts)
        -------------------------------------------------------------------------
        local grp = api.nvim_create_augroup("ForceIpynbInterpretedAsMarkdown", { clear = true })
        local function force_markdown(buf)
            if not buf or not api.nvim_buf_is_valid(buf) then return end
            local name = api.nvim_buf_get_name(buf)
            if name:match("%.ipynb_interpreted$") and vim.bo[buf].filetype ~= "markdown" then
                api.nvim_buf_call(buf, function()
                    cmd("setfiletype markdown")
                end)
            end
        end

        api.nvim_create_autocmd({ "BufRead", "BufNewFile" }, {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
                vim.defer_fn(function() force_markdown(args.buf) end, 0)
            end,
        })

        api.nvim_create_autocmd("FileType", {
            group = grp,
            pattern = "*",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })

        api.nvim_create_autocmd("OptionSet", {
            group = grp,
            pattern = "filetype",
            callback = function()
                local buf = api.nvim_get_current_buf()
                force_markdown(buf)
            end,
        })

        api.nvim_create_autocmd("BufEnter", {
            group = grp,
            pattern = "*.ipynb_interpreted",
            callback = function(args)
                force_markdown(args.buf)
            end,
        })

        -------------------------------------------------------------------------
        -- Syntax highlight for interpreted notebooks
        -------------------------------------------------------------------------
        api.nvim_create_autocmd("FileType", {
            group = api.nvim_create_augroup("IpynbInterpretedSyntax", { clear = true }),
            pattern = "markdown",
            callback = function(args)
                local bufname = api.nvim_buf_get_name(args.buf)
                if not bufname:match("%.ipynb_interpreted$") then return end
                vim.schedule(function()
                    cmd([[
                        syntax match IPYNBCellTag /^<cell>$/ containedin=ALL
                        syntax match IPYNBCellTag /^<\/cell>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<output>$/ containedin=ALL
                        syntax match IPYNBOutputTag /^<\/output>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<markdown>$/ containedin=ALL
                        syntax match IPYNBMarkdownTag /^<\/markdown>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<raw>$/ containedin=ALL
                        syntax match IPYNBRawTag /^<\/raw>$/ containedin=ALL

                        syntax include @Python syntax/python.vim
                        syntax region IPYNBPython start=/^<cell>$/ end=/^<\/cell>$/ contains=@Python keepend
                        syntax region IPYNBRawContent start=/^<raw>$/ end=/^<\/raw>$/ contains=IPYNBRawText keepend
                        syntax region IPYNBOutputContent start=/^<output>$/ end=/^<\/output>$/ contains=IPYNBOutputText keepend
                        syntax match IPYNBOutputText /.*/ contained
                        syntax match IPYNBRawText /.*/ contained

                        syntax match IPYNBEvalRunning /\v\[\*\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalDone /\v\[Done\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalError /\v\[Error\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Restarted /\v\[Kernel_Restarted\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Interrupted /\v\[Kernel_Interrupted\]/ containedin=IPYNBOutputText
                        syntax match IPYNBEvalKernel_Stopped /\v\[Kernel_Stopped\]/ containedin=IPYNBOutputText

                        highlight IPYNBCellTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBMarkdownTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBRawTag guifg=#5e5e5e ctermfg=240 gui=italic cterm=italic
                        highlight IPYNBOutputText gui=NONE cterm=NONE
                        highlight IPYNBRawText guifg=#dddddd ctermfg=252
                        highlight IPYNBEvalRunning guifg=orange ctermfg=208
                        highlight IPYNBEvalDone guifg=green ctermfg=34
                        highlight IPYNBEvalError guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Restarted guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Interrupted guifg=red ctermfg=196
                        highlight IPYNBEvalKernel_Stopped guifg=red ctermfg=196
                    ]])
                end)
            end,
        })
    end,
}
```

Add these lines to setup the keymaps '~/.config/nvim/init.lua':
```
vim.api.nvim_create_autocmd("FileType", {
  pattern = "ipynb_interpreted",
  callback = function()
	vim.keymap.set("n", "<Space>", "<Nop>", { desc = "Disable Space default behavior" })
	vim.keymap.set("n", "<CR>", function() vim.cmd("VolcanoEvaluate") end, { desc = "Run current cell" })
	vim.keymap.set("n", "<leader><CR>", function() vim.cmd("VolcanoEvaluateJump") end, { desc = "Run current cell and jump" })
	vim.keymap.set("n", "<leader>k<CR>", function() vim.cmd("VolcanoEvaluateAbove") end, { desc = "Run cells above" })
	vim.keymap.set("n", "<leader>j<CR>", function() vim.cmd("VolcanoEvaluateBelow") end, { desc = "Run cells below" })
	vim.keymap.set("n", "<leader><leader><CR>", function() vim.cmd("VolcanoEvaluateAll") end, { desc = "Run all cells" })
	vim.keymap.set("n", "<leader>do<CR>", function() vim.cmd("VolcanoDeleteOutput") end, { desc = "Delete output" })
	vim.keymap.set("n", "<leader><leader>do<CR>", function() vim.cmd("VolcanoDeleteAllOutputs") end, { desc = "Delete all outputs" })
	vim.keymap.set("n", "<leader>dok<CR>", function() vim.cmd("VolcanoDeleteOutputsAbove") end, { desc = "Delete outputs above" })
	vim.keymap.set("n", "<leader>doj<CR>", function() vim.cmd("VolcanoDeleteOutputsBelow") end, { desc = "Delete outputs below" })
	vim.keymap.set("n", "<leader>nck<CR>", function() vim.cmd("VolcanoCreateCellUpward") end, { desc = "Create new cell above" })
	vim.keymap.set("n", "<leader>ncj<CR>", function() vim.cmd("VolcanoCreateCellDownward") end, { desc = "Create new cell below" })
	vim.keymap.set("n", "<leader>.", function() vim.cmd("VolcanoSwitchCellTypeForward") end, { desc = "Next cell type" })
	vim.keymap.set("n", "<leader>,", function() vim.cmd("VolcanoSwitchCellTypeBackward") end, { desc = "Previous cell type" })
	vim.keymap.set("n", "<leader>mck<CR>", function() vim.cmd("VolcanoMoveCellUpward") end, { desc = "Move cell up" })
	vim.keymap.set("n", "<leader>mcj<CR>", function() vim.cmd("VolcanoMoveCellDownward") end, { desc = "Move cell down" })
	vim.keymap.set("n", "<leader>dc<CR>", function() vim.cmd("VolcanoDeleteCell") end, { desc = "Delete cell" })
	vim.keymap.set("n", "<leader>cc<CR>", function() vim.cmd("VolcanoCopyCell") end, { desc = "Copy cell" })
	vim.keymap.set("n", "<leader>pc<CR>", function() vim.cmd("VolcanoPasteCell") end, { desc = "Paste cell" })
	vim.keymap.set("n", "<leader>ik<CR>", function() vim.cmd("VolcanoInterrupt") end, { desc = "Interrupt Kernel" })
	vim.keymap.set("n", "<leader>rk<CR>", function() vim.cmd("VolcanoRestart") end, { desc = "Restart Kernel" })
	vim.keymap.set("n", "<leader>rkdo<CR>", function() vim.cmd("VolcanoRestartAndDeleteAllOutput") end, { desc = "Restart Kernel and Clear Output" })
	vim.keymap.set("n", "<leader><leader>rk<CR>", function() vim.cmd("VolcanoRestartAndEvaluateAll") end, { desc = "Restart Kernel and Run All Cells" })
	vim.keymap.set("n", "<leader>rkc<CR>", function() vim.cmd("VolcanoRestartAndEvaluateUpToCursor") end, { desc = "Restart Kernel and Run Up To Cursor" })
	vim.keymap.set("n", "<leader>vi<CR>", function() vim.cmd("VolcanoInfo") end, { desc = "Show information about kernels" })
  end,
})
```

## Credits

- [Molten](https://github.com/benlubas/molten-nvim): Original fork, GPL-licensed, basis for project.
- [render-markdown.nvim](https://github.com/MeanderingProgrammer/render-markdown.nvim): Core function copied and modified for specific use case.
- [nvim-treesitter](https://github.com/nvim-treesitter/nvim-treesitter): Used for syntax coloring

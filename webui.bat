@echo off
setlocal
cd /d "%~dp0"

rem Optional local settings may set PYTHON, VENV_DIR and COMMANDLINE_ARGS.
if exist webui.settings.bat call webui.settings.bat

if not defined VENV_DIR set "VENV_DIR=.venv"
set "LLM_VENV=.venv-llm"
set "PY_STAMP=%VENV_DIR%\.install-stamp"
set "RELEASE_CONSTRAINTS=constraints-release.txt"
set "LLM_CONSTRAINTS=constraints-llm.txt"
set "NPM_STAMP=frontend\node_modules\.install-stamp"
set "DIST=frontend\dist\index.html"

set "DO_UPDATE=0"
set "DO_DEV=0"
set "DO_LLM=0"
set "SKIP_BUILD=0"
set "SERVE_ARGS="

:parse_args
if "%~1"=="" goto :prerequisites
if "%~1"=="--update" set "DO_UPDATE=1"& shift& goto :parse_args
if "%~1"=="--dev" set "DO_DEV=1"& shift& goto :parse_args
if "%~1"=="--llm" set "DO_LLM=1"& shift& goto :parse_args
if "%~1"=="--skip-build" set "SKIP_BUILD=1"& shift& goto :parse_args
if "%~1"=="-h" goto :help
if "%~1"=="--help" goto :help
set "SERVE_ARGS=%SERVE_ARGS% %1"
shift
goto :parse_args

:prerequisites
set "HAVE_UV=0"
where uv >NUL 2>NUL && set "HAVE_UV=1"

set "PYTHON_BIN="
set "PYTHON_ARGS="
set "PYTHON_OVERRIDE_USED=0"
set "PYTHON_OVERRIDE_OUTCOME=not set"
if defined PYTHON (
    "%PYTHON%" -m backend.cli starter python >NUL 2>NUL
    if not errorlevel 1 (
        set "PYTHON_BIN=%PYTHON%"
        set "PYTHON_OVERRIDE_USED=1"
        goto :python_found
    )
    set "PYTHON_OVERRIDE_OUTCOME=found but rejected"
)

set "PY_PATH="
for /f "delims=" %%I in ('where py 2^>NUL') do if not defined PY_PATH set "PY_PATH=%%I"
set "PY_313_OUTCOME=not found"
if defined PY_PATH set "PY_313_OUTCOME=found at %PY_PATH% but rejected"
py -3.13 -m backend.cli starter python >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_BIN=py"
    set "PYTHON_ARGS=-3.13"
    goto :python_found
)

set "PY_312_OUTCOME=not found"
if defined PY_PATH set "PY_312_OUTCOME=found at %PY_PATH% but rejected"
py -3.12 -m backend.cli starter python >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_BIN=py"
    set "PYTHON_ARGS=-3.12"
    goto :python_found
)

set "PYTHON_PATH="
for /f "delims=" %%I in ('where python 2^>NUL') do if not defined PYTHON_PATH set "PYTHON_PATH=%%I"
set "PYTHON_OUTCOME=not found"
if defined PYTHON_PATH set "PYTHON_OUTCOME=found at %PYTHON_PATH% but rejected"
python -m backend.cli starter python >NUL 2>NUL
if not errorlevel 1 (
    set "PYTHON_BIN=python"
    goto :python_found
)
if defined PYTHON_PATH call :classify_python_path

:python_found
if "%HAVE_UV%"=="0" if not defined PYTHON_BIN (
    call :python_not_found
    exit /b 1
)
if "%HAVE_UV%"=="0" call :warn "uv not found, falling back to venv + pip. Install uv for faster setup: https://docs.astral.sh/uv/"

set "HAVE_NODE=0"
where npm >NUL 2>NUL && set "HAVE_NODE=1"

if "%DO_UPDATE%"=="1" (
    where git >NUL 2>NUL
    if errorlevel 1 (
        call :die "--update needs git."
        exit /b 1
    )
    call :say "Pulling the latest revision"
    git pull --ff-only || exit /b 1
    if exist "%PY_STAMP%" del /q "%PY_STAMP%"
    if exist "%NPM_STAMP%" del /q "%NPM_STAMP%"
    if "%HAVE_NODE%"=="1" if exist "%DIST%" del /q "%DIST%"
)

rem --- python environment ---------------------------------------------------

if not exist "%VENV_DIR%\Scripts\livebound.exe" goto :install_python
if not exist "%PY_STAMP%" goto :install_python_done
goto :python_ready

:install_python
if not exist "%VENV_DIR%" (
    call :say "Creating %VENV_DIR%"
    if "%HAVE_UV%"=="1" (
        if "%PYTHON_OVERRIDE_USED%"=="1" (
            uv venv --python "%PYTHON_BIN%" "%VENV_DIR%" || exit /b 1
        ) else (
            uv venv "%VENV_DIR%" || exit /b 1
        )
    ) else (
        "%PYTHON_BIN%" %PYTHON_ARGS% -m venv "%VENV_DIR%" || exit /b 1
    )
)

:install_python_done
call :say "Installing the application"
if "%HAVE_UV%"=="1" (
    uv pip install --python "%VENV_DIR%\Scripts\python.exe" -e . --group dev -c "%RELEASE_CONSTRAINTS%" --build-constraints "%RELEASE_CONSTRAINTS%" 2>NUL
    if errorlevel 1 uv pip install --python "%VENV_DIR%\Scripts\python.exe" -e . -c "%RELEASE_CONSTRAINTS%" --build-constraints "%RELEASE_CONSTRAINTS%" || exit /b 1
) else (
    "%VENV_DIR%\Scripts\python.exe" -m pip install --quiet --upgrade pip || exit /b 1
    "%VENV_DIR%\Scripts\python.exe" -m pip install -e . -c "%RELEASE_CONSTRAINTS%" --build-constraint "%RELEASE_CONSTRAINTS%" || exit /b 1
)
type NUL >"%PY_STAMP%"

:python_ready
call "%VENV_DIR%\Scripts\activate.bat" || exit /b 1
"%VENV_DIR%\Scripts\python.exe" -m backend.cli starter python --interpreter "%VENV_DIR%\Scripts\python.exe" --environment "%VENV_DIR%" || exit /b 1

if "%DO_LLM%"=="1" call :install_llm || exit /b 1

rem --- frontend ------------------------------------------------------------

set "STARTER_FRONTEND_ARGS="
if "%HAVE_NODE%"=="1" set "STARTER_FRONTEND_ARGS=%STARTER_FRONTEND_ARGS% --npm"
if "%DO_DEV%"=="1" set "STARTER_FRONTEND_ARGS=%STARTER_FRONTEND_ARGS% --dev"
if "%DO_UPDATE%"=="1" set "STARTER_FRONTEND_ARGS=%STARTER_FRONTEND_ARGS% --update"
if "%SKIP_BUILD%"=="1" set "STARTER_FRONTEND_ARGS=%STARTER_FRONTEND_ARGS% --skip-build"

"%VENV_DIR%\Scripts\python.exe" -m backend.cli starter frontend %STARTER_FRONTEND_ARGS% || exit /b 1
"%VENV_DIR%\Scripts\python.exe" -m backend.cli starter frontend %STARTER_FRONTEND_ARGS% --decision dependencies
if not errorlevel 1 (
    call :say "Installing frontend dependencies"
    pushd frontend
    call npm install
    if errorlevel 1 (
        popd
        exit /b 1
    )
    popd
    type NUL >"%NPM_STAMP%"
)

"%VENV_DIR%\Scripts\python.exe" -m backend.cli starter frontend %STARTER_FRONTEND_ARGS% --decision build
if not errorlevel 1 (
    call :say "Building the frontend"
    pushd frontend
    call npm run build
    if errorlevel 1 (
        popd
        exit /b 1
    )
    popd
)

"%VENV_DIR%\Scripts\python.exe" -m backend.cli starter dependencies --launcher webui.bat

rem --- run -----------------------------------------------------------------

if "%DO_DEV%"=="1" (
    if "%HAVE_NODE%"=="0" (
        call :die "--dev needs npm."
        exit /b 1
    )
    call :say "Backend on http://127.0.0.1:8430, dev server below. Ctrl-C stops both."
    start "" /b "%VENV_DIR%\Scripts\python.exe" -m backend.cli serve --no-browser %COMMANDLINE_ARGS% %SERVE_ARGS%
    pushd frontend
    call npm run dev
    if errorlevel 1 (
        popd
        exit /b 1
    )
    popd
    exit /b 0
)

call :say "Ctrl-C stops Livebound. Windows will then ask whether to end the batch job; that is expected, answer yes."
"%VENV_DIR%\Scripts\python.exe" -m backend.cli serve %COMMANDLINE_ARGS% %SERVE_ARGS%
exit /b %ERRORLEVEL%

:install_llm
if not exist "%LLM_VENV%" (
    call :say "Creating %LLM_VENV% (this pulls a multi-gigabyte ML stack)"
    if "%HAVE_UV%"=="1" (
        if "%PYTHON_OVERRIDE_USED%"=="1" (
            uv venv --python "%PYTHON_BIN%" "%LLM_VENV%" || exit /b 1
        ) else (
            uv venv "%LLM_VENV%" || exit /b 1
        )
    ) else (
        "%PYTHON_BIN%" %PYTHON_ARGS% -m venv "%LLM_VENV%" || exit /b 1
    )
)
call :say "Installing the model worker requirements"
if "%HAVE_UV%"=="1" (
    uv pip install --python "%LLM_VENV%\Scripts\python.exe" -r requirements-llm.txt -c "%LLM_CONSTRAINTS%" || exit /b 1
) else (
    "%LLM_VENV%\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
    "%LLM_VENV%\Scripts\python.exe" -m pip install -r requirements-llm.txt -c "%LLM_CONSTRAINTS%" || exit /b 1
)
call :say "Point 'LLM interpreter' in the settings at %CD%\%LLM_VENV%\Scripts\python.exe"
exit /b 0

:say
echo :: %~1
exit /b 0

:warn
echo :: %~1 1>&2
exit /b 0

:die
echo :: %~1 1>&2
exit /b 1

:python_not_found
call :warn "No usable Python 3.12 or newer interpreter was found. Candidates tried:"
if defined PYTHON call :warn "  PYTHON=%PYTHON%: %PYTHON_OVERRIDE_OUTCOME%"
call :warn "  py -3.13: %PY_313_OUTCOME%"
call :warn "  py -3.12: %PY_312_OUTCOME%"
call :warn "  python: %PYTHON_OUTCOME%"
if defined PYTHON_PATH_WITHOUT_WINDOWS_APPS if not "%PYTHON_PATH_WITHOUT_WINDOWS_APPS%"=="%PYTHON_PATH%" call :warn "  Switch off App execution aliases in Settings > Apps > Advanced app settings > App execution aliases, or set PYTHON to your real interpreter."
call :die "Set PYTHON=C:\Users\your-name\AppData\Local\Programs\Python\Python312\python.exe, or install Python 3.12 or newer."
exit /b 1

:classify_python_path
set "PYTHON_PATH_WITHOUT_WINDOWS_APPS=%PYTHON_PATH:\WindowsApps=%"
if not "%PYTHON_PATH_WITHOUT_WINDOWS_APPS%"=="%PYTHON_PATH%" set "PYTHON_OUTCOME=found at %PYTHON_PATH% but rejected: it is a Microsoft Store placeholder, not an interpreter"
exit /b 0

:help
echo Set up whatever is missing, then start Livebound.
echo.
echo   webui.bat                start (set up first if needed)
echo   webui.bat --dev          plus the Vite dev server with hot reload
echo   webui.bat --update       git pull, reinstall, rebuild, then start
echo   webui.bat --llm          also set up .venv-llm for the local model
echo   webui.bat --skip-build   do not touch the frontend at all
exit /b 0

# Kyty Live

A local tool for the KytyPS5 fix loop: run a game, see what fails as it happens, change the
emulator, then rebuild and run the same game again with one button. Each run is compared with the
previous run of the same game.

It lives outside the KytyPS5 repository on purpose. It only drives the installed emulator.

## Start

Double-click `Kyty Live.cmd`, or run:

```
python run.py
```

Then open http://127.0.0.1:8790. Python 3.10+ standard library only. Windows only for window
tracking and key input.

## What it does

- **Run** starts `kyty_emulator.exe` from the install folder with `--printf-direction Console`.
  Without that flag the emulator logs almost nothing: the whole log is gated on it and it defaults
  to `Silent`.
- **Failures** are deduplicated with a count:
  - Critical: fatal errors (`--- Fatal Error ---` / `--- Error ---` blocks, with file:line), host
    crashes, and missing functions the game actually called.
  - Errors: Vulkan validation messages and stalls (no new frame for 10 s).
  - Warnings: missing imports and unresolved `dlsym` lookups. Info: missing files.
  - NIDs are named from `data/nids.json`. "AnyPS5 has it" marks a function AnyPS5 lists as done.
  - "New" marks a failure that the previous run of the same game did not have.
- **Frame, fps and time** come from the emulator window title.
- **Rebuild & rerun** stops the game, runs `cmake --build` and `cmake --install` in the build
  folder, then starts the same game again.
- **Controller** sends pad keys to the emulator window (J = Cross, Enter = Options, and so on).
  Space and Esc are left out on purpose, because they pause and quit the emulator.
- **Boot macro**: one step per line, `<seconds after launch> <key>`. It is replayed on every run so
  each rerun reaches the same point. Tick "Record presses" to build it from the Controller buttons.
- **Runs** are saved in `runs/<time>_<title id>/`: `output.log` (the full log), `issues.json`
  and `meta.json` (commit, number of changed files, exit code, highest frame).

## Hot patch (replace HLE functions while the game runs)

Needs an emulator built from the KytyPS5 branch `feat/hot-hle` (`src/loader/hotHle.cpp`).
`config.json` points Kyty Live at that build (`_Build/dev`).

1. Write a function in `hot/patch.cpp`:

   ```cpp
   HOT_FUNC(sceKernelUsleep, int, (uint32_t usec)) { ... }                // by name
   HOT_FUNC_SYMBOL("_ZN3sce2Np9CppWebApi6Common10InitParamsC1Ev",
                   InitParamsCtor, void, (void* self)) { ... }            // C++ symbol
   HOT_FUNC_NID("fzyMKs9kim0", WaitEqueue, int, (...)) { ... }            // NID only
   ```

   `hot::log(...)` writes into the emulator log. `hot::original<Fn>(nid)` returns the emulator's
   own function, so a patch can wrap it as well as replace it.
2. Save. Kyty Live compiles `hot/build/kyty_hot.dll` (under a second) and swaps it in atomically.
3. The emulator (started with `KYTY_HOT_HLE=hot/build`) sees the new DLL, loads a copy, and points
   the game's import slots for those functions at it. A function you remove goes back to the
   emulator's version. The Hot patch panel shows what is redirected; matching failures get a
   "Patched" tag.

A patch that already exists when the game starts is applied before the game's first call, so
functions called once during boot can be patched too.

Limits:
- Only calls through import slots are redirected. Addresses the game got from `dlsym`, or a
  function pointer it copied earlier, keep the old target.
- Each saved version starts with fresh static state in the patch.
- A crash in a patch crashes the game. There are no savestates, so fix it, then use Rebuild &
  rerun or Run.
- When a patch works, move the code into the real library in KytyPS5 and rebuild. The patch is
  a workbench, not where fixes live.

## Shader override (replace one shader's SPIR-V while the game runs)

Needs an emulator built from `feat/hot-hle` with `pipeline/shaderOverride.cpp`. Kyty Live sets
`KYTY_SHADER_OVERRIDE=shaders/` when it launches a game.

- Every shader the translator produces is listed in the Shaders panel and written to
  `shaders/dump/<stage>_<guest hash>_<spirv hash>.spv` and `.spvasm` (readable SPIR-V assembly).
- **Edit** copies the `.spvasm` to `shaders/override/` and opens it. Save it: the emulator
  assembles and validates it, and pipelines that use the shader are rebuilt on their next draw.
  **Revert** deletes the copy and the translator's own output comes back.
- An override that does not assemble or validate is rejected with the reason; the game keeps the
  shader it is running.
- **Snapshot** saves the game window as a PNG in the run folder, to compare before and after.

Things to know:
- The name ends in a hash of the translator's output. After a translator change, the shader gets
  a new name and old overrides stop applying, which is deliberate: an override only replaces the
  exact SPIR-V it was made from.
- Keep the shader's interface. An override that adds a capability, or changes any Location,
  Binding, DescriptorSet or BuiltIn of a variable, is rejected with what changed, because the
  pipeline built around the translated shader would not accept it. Changes the check cannot see
  (for example a different push-constant layout) can still fail pipeline creation and stop the
  emulator.
- Find the shader to edit by its guest hash: the log prints it (`ShaderRecompiler PS ... hash=0x...`),
  and the search box takes any part of it.
- When the edit works, make the same change in the translator. The override is a workbench, not
  where fixes live.

## Configuration

Kyty Live works without any configuration when the KytyPS5 checkout sits next to it:

```
GitHub/
  KytyPS5/      emulator source; Kyty Live builds it into _Build/kyty-live
  kyty-live/    this tool
```

To change anything, copy `config.example.json` to `config.json` (it is not committed) and keep
only the keys you need. Relative paths start at Kyty Live's folder.

| Key | Default |
|---|---|
| `src_dir` | `KYTY_SRC_DIR` if set, else `../KytyPS5` |
| `build_dir` | `<src_dir>/_Build/kyty-live` |
| `install_dir` | `<build_dir>/install` |
| `game_dirs` | the KytyPS5 launcher's list in `%ProgramData%\Kyty\Kyty.ini` |
| `vcvars`, `symbolizer`, `clang_cl`, `cxxfilt` | found in the newest Visual Studio install with the C++ tools (via `vswhere`) |
| `port` | `8790` |
| `emulator_args` | `["--no-splash", "--printf-direction", "Console"]` |
| `stall_seconds` | `10` |
| `hot_hle`, `shader_override` | `true` |

## Updating NID names

`data/nids.json` (about 412,000 names; not committed, build it with `python tools/build_nids.py`) is built from the coverage spreadsheet plus the public
symbol lists in `data/symbols/`: `aerolib.csv`, `ps5_names.txt` and `unique.txt` from
zecoxao/sce_symbols, and `ps5_symbols.txt` from flatz/ida_ps5_elf_plugin. Every name is hashed
and kept only if it produces its NID. C++ names get a readable form from `llvm-cxxfilt`; hover
over a failure to see the raw symbol.

```
python tools/build_nids.py ../KytyPS5_vs_AnyPS5_function_coverage.xlsx
```

## Tests

```
python -m unittest discover -s tests -t .
```

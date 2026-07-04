# Call of Duty Black Ops 2 Translation Toolkit

A toolkit for translating **Call of Duty: Black Ops II** text and preparing replacement font assets.

## Text Workflow

1. Open the toolkit and select the Black Ops II installation folder.
2. Open **Extract** and choose an output folder.
3. Extract text as one TXT file or as a folder of TXT files.
4. Translate the TXT without deleting or renaming its manifest.
5. Keep every text entry on one physical line. Use `\n` for in-game line breaks.
6. Open **Pack**, select the translated output folder, and choose a package output folder.
7. Copy the generated `xinput1_3.dll` and `.bin` file next to `t6sp.exe`.

## Fonts

Font export contains the editable 720 PC font atlas:

- `fonts/atlases/gamefonts_pc_720.png`
- `fonts/metrics/fonts/720/*.csv`

Edit the PNG and, if needed, the matching metric CSV files. Use **Pack** to build `font.bin` or `all.bin`.

## Package Files

- `text.bin` contains text only.
- `font.bin` contains fonts only.
- `all.bin` contains text and fonts together.

The game can load either `all.bin`, or separate `text.bin` and `font.bin`, next to `xinput1_3.dll`.

## Files

Keep these files in the same folder:

- `cod_bo2_translation_toolkit.py` or the compiled executable
- `lib/OpenAssetTools/Unlinker.exe`
- `lib/OpenAssetTools/raw/t6/partclassification.csv`
- `lib/OpenAssetTools/raw/t6/partclassification_mp.csv`
- `lib/dll/xinput1_3.dll`

## Requirements

- Python 3.10 or newer when running the source script
- Pillow when running the source script

## Credits

- **Laupetin** - OpenAssetTools, used for Black Ops II fastfile dumping.
- **OpenAssetTools contributors** - Call of Duty asset extraction research and tooling.

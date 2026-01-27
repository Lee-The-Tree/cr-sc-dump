# cr-sc-dump
Python scripts to extract textures and reconstruct sprites from Supercell’s `*.sc`, `*_dl.sc`, `*_tex.sc`, `*.csv`, `*.ktx`, and `*.sctx` files.

## Installation
```console
pip install -r requirements.txt
```

## Usage

### 1. Extract Textures (`dumpsc.py`)
Use this to extract raw PNG textures from `.sc` or `_tex.sc` files. It handles modern compression (Zstandard, LZHAM) and various pixel formats (ASTC, ETC1, RGBA8888, etc.).

**Batch Processing:**
You can pass multiple files or entire directories to process them in batches.
```console
# Process all files in a folder
python dumpsc.py path/to/sc_folder/

# Process multiple specific files
python dumpsc.py file1_tex.sc file2_tex.sc
```

**Legacy Files:**
If you are missing `_tex.sc` files or are working with older `_dl.sc` files, use the `--old` flag:
```console
python dumpsc.py path/to/filename_dl.sc --old
```

### 2. Reconstruct Sprites (`sc_decode.py`)
Use this to cut and reconstruct individual sprites from the extracted textures using the polygon data in the `.sc` files.

**Note:** Requires the corresponding `*_tex.png` files (extracted via `dumpsc.py`) to be in the same directory as the `.sc` file.

```console
# Reconstruct sprites from an SC file
python sc_decode.py -s path/to/ui.sc
```
The reconstructed sprites will be saved in a folder named `<filename>_out`.

## Credits
* [athlan20](https://github.com/athlan20)
* [clanner](https://github.com/clanner)
* [Galaxy1036](https://github.com/Galaxy1036)
* [umop-aplsdn](https://github.com/umop-aplsdn)

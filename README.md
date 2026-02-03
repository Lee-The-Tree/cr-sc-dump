# cr-sc-dump
Python scripts to extract textures and reconstruct sprites from Supercell’s `*.sc`, `*_dl.sc`, `*_tex.sc`, `*.csv`, `*.ktx`, and `*.sctx` files.

## Installation
```console
pip install -r requirements.txt
```

## Usage

### Unified Tool (`dumpsc.py`)
The tool extracts textures and reconstructs sprites from SC files. It handles standard SC files, `_dl` files, and external texture files (`_tex.sc`).

**Basic Usage:**
```console
# Extract textures only
python dumpsc.py path/to/file.sc

# Extract textures AND reconstruct sprites
python dumpsc.py -s path/to/file.sc
```

**Batch Processing:**
You can pass a directory to process all files within it.
```console
# Process all files in a folder (extract textures + reconstruct sprites)
python dumpsc.py -s path/to/assets/sc/
```

**Output Directory:**
Specify an output folder using `-o`. The script will create subfolders for each file, consolidating related files (e.g., `base.sc` and `base_tex.sc`) into a single output directory.
```console
python dumpsc.py -s path/to/assets/sc/ -o ./output_folder/
```

**Export Layers:**
To export individual layers for each sprite (useful for editing in Photoshop/GIMP), use the `--layers` flag. This will create a `_layers` subdirectory for each sprite containing the component images.
```console
python dumpsc.py -s path/to/file.sc --layers
```

**Re-running on Output:**
You can run the script on a folder that already contains extracted textures. The script will skip re-extraction if the files exist (or overwrite them) and proceed to sprite reconstruction.
```console
# Re-run sprite reconstruction on the output folder
python dumpsc.py -s ./output_folder/ -o ./output_folder/ --sprites-only
```

**Advanced Options:**
*   `--old`: Force "old" header parsing logic (useful for some `_dl.sc` files if auto-detection fails).
*   `--group`: Group output files by type (e.g., `ui`, `chr`, `spell`) in the output directory.
*   `--sprites-only`: Skip the texture extraction pass and only run sprite reconstruction (requires textures to be present).
*   `--verbose`: Enable verbose logging for debugging.

## Output Structure
For a file named `chr_golem.sc`, the output will be:
```
output_folder/
  chr_golem/
    chr_golem_0.png       # Extracted texture atlas
    chr_golem.json        # Texture atlas definition (JSON)
    sprites/
      chr_golem_sprite_00.png  # Reconstructed sprite
      ...
      chr_golem_sprite_00_layers/  # (If --layers is used)
        layer_0.png
        layer_1.png
        ...
```

## Credits
* [athlan20](https://github.com/athlan20)
* [clanner](https://github.com/clanner)
* [Galaxy1036](https://github.com/Galaxy1036)
* [umop-aplsdn](https://github.com/umop-aplsdn)

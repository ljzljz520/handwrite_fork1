# Getting Started with Handwrite!

## Creating your Handwritten Sample

1. Take a printout of the [sample form](https://github.com/yashlamba/handwrite/raw/main/handwrite_sample.pdf).

2. Fill the form using the image below as a reference.

3. Scan the filled form using a scanner, or Adobe Scan in your phone.

4. Save the `.jpg` image in your system.

Your form should look like this:

<p align="center">
        <img src="https://raw.githubusercontent.com/builtree/assets/handwrite/handwrite_filled_form.jpg" width=50%>
        </img>
</p>

## Creating your font

1.  Make sure you have installed `handwrite`, `potrace` & `fontforge`.

2.  In a terminal type `handwrite [PATH TO IMAGE] [OUTPUT DIRECTORY]`.
    (You can also type `handwrite -h`, to see all the arguments you can use).

3.  (Optional) Config file containing custom options for your font can also be passed using
    the `--config [CONFIG FILE]` argument.

        ???+ note - If you expicitly pass the metadata (filename, family or style) as CLI arguments, they are given a preference over the default config file data.

             - If no config file is provided for an input then the [default config file](https://github.com/yashlamba/handwrite/blob/main/handwrite/default.json) is used.

    yashlamba

4.  Your font will be created as `OUTPUT DIRECTORY/OUTPUT FONT NAME.ttf`. Install the font in your system.

5.  Select your font in your word processor and get to work!
    Here's the end result!

<p align="center">
        <img src="https://raw.githubusercontent.com/builtree/assets/handwrite/handwrite_sentence.png">
        </img>
</p>

## Continuing a build (`resume`)

A normal run deletes the intermediate character files when it finishes (they
live in a temporary directory). To fix individual glyphs and regenerate the
font without redoing every character, keep the intermediate files around and
continue the build afterwards:

1.  Run the initial build with `--directory`:

    ```
    handwrite sample.jpg output/ --directory work/ --config my_config.json
    ```

    Besides the `<codepoint>/<codepoint>.png`, `.bmp` and `.svg` files, `work/`
    now contains a hidden `.handwrite/` state folder. For every codepoint it
    records the original crop from the sheet, any manual modification, and the
    outline (SVG) the font was last built from. Do not delete it.

2.  Edit any glyph files you want:

    - edit `work/<codepoint>/<codepoint>.png` to fix a crop, or
    - edit `work/<codepoint>/<codepoint>.svg` to fix an outline directly.

3.  Continue the build with the `resume` command:

    ```
    handwrite resume --directory work/
    ```

    Only affected stages are rebuilt: a changed PNG re-traces that codepoint's
    BMP/SVG, a changed SVG is taken as-is, and the font is regenerated only
    when an outline, the config or the metadata actually changed. All other
    glyphs stay byte-for-byte identical.

    Use `--sheet`, `--config`, `--output-directory`, `--filename`, `--family`
    and `--style` to point at a newer scan/config (the paths from the previous
    run are reused by default).

4.  **Conflicts are never resolved silently.** If a new scan or a changed
    threshold would replace a PNG/SVG you edited by hand, `resume` lists every
    affected codepoint and exits without touching any files. Either keep the
    edit (use `resume` without the new source) or rerun with `--force` to
    accept the new crop; the overwritten edits are copied to
    `work/.handwrite/backup/<run-id>/` first.

    Re-running the original one-shot command with `--directory` onto a folder
    that contains edits is likewise refused unless you pass `--force`.

5.  An interrupted run is not trusted as a result: partial files are written
    through temporary files and moved into place atomically, the font is
    regenerated into a hidden temporary file and renamed only on success, and
    the next `resume` cleans stale temporary files up before starting.

## Configuring

TO DO

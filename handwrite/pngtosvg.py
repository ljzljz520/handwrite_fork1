from PIL import Image, ImageChops
import io
import os
import shutil
import subprocess
import tempfile

# BMP stage constants. The resume manifest records these so a change in the
# rasterisation parameters invalidates previously built derivatives.
BMP_SIZE = 100
BMP_THRESHOLD = 200


class PotraceNotFound(Exception):
    pass


class PNGtoSVG:
    """Converter class to convert character PNGs to BMPs and SVGs."""

    def convert(self, directory):
        """Call converters on each .png in the provider directory.

        Walk through the custom directory containing all .png files
        from sheettopng and convert them to png -> bmp -> svg.
        """
        path = os.walk(directory)
        for root, dirs, files in path:
            for f in files:
                if f.endswith(".png"):
                    self.convert_png(root + "/" + f)

    def convert_png(self, path, atomic=False):
        """Convert a single ``.png`` to ``.bmp`` and then ``.svg``.

        Parameters
        ----------
        path : str
            Path to the png file to be converted.
        atomic : bool, default=False
            When True, write the bmp/svg to temporary files first and move
            them into place only after the conversion succeeded. This keeps
            interrupted resume runs from leaving partial derivatives.
        """
        self.pngToBmp(path, atomic=atomic)
        # self.trim(path[0:-4] + ".bmp")
        self.bmpToSvg(path[0:-4] + ".bmp", atomic=atomic)

    def bmpToSvg(self, path, atomic=False):
        """Convert .bmp image to .svg using potrace.

        Converts the passed .bmp file to .svg using the potrace
        (http://potrace.sourceforge.net/). Each .bmp is passed as
        a parameter to potrace which is called as a subprocess.

        Parameters
        ----------
        path : str
            Path to the bmp file to be converted.
        atomic : bool, default=False
            When True, move the generated svg into place only after potrace
            succeeds, instead of letting potrace write the final path itself.

        Raises
        ------
        PotraceNotFound
            Raised if potrace not found in path by shutil.which()
        """
        if shutil.which("potrace") is None:
            raise PotraceNotFound("Potrace is either not installed or not in path")

        out_svg = path[0:-4] + ".svg"
        if not atomic:
            subprocess.run(["potrace", path, "-b", "svg", "-o", out_svg])
            return

        tmp_svg = _temp_path(out_svg, ".svg")
        try:
            process = subprocess.run(["potrace", path, "-b", "svg", "-o", tmp_svg])
            if process.returncode != 0 or not _is_nonempty_file(tmp_svg):
                raise RuntimeError("potrace failed to convert {}".format(path))
            os.replace(tmp_svg, out_svg)
        finally:
            if os.path.exists(tmp_svg):
                os.remove(tmp_svg)

    def render_bmp_bytes(self, source):
        """Return the thresholded/resized ``.bmp`` bytes for a PNG source.

        Parameters
        ----------
        source : str or bytes
            Path to the png file, or the raw png bytes (e.g. a freshly
            detected crop that has not been written to disk yet).
        """
        if isinstance(source, bytes):
            img = Image.open(io.BytesIO(source)).convert("RGBA")
        else:
            img = Image.open(source).convert("RGBA")
        img = img.resize((BMP_SIZE, BMP_SIZE))

        # Threshold image to convert each pixel to either black or white
        threshold = BMP_THRESHOLD
        data = []
        for pix in img.get_flattened_data():
            if pix[0] >= threshold and pix[1] >= threshold and pix[3] >= threshold:
                data.append((255, 255, 255, 0))
            else:
                data.append((0, 0, 0, 1))
        img.putdata(data)
        buffer = io.BytesIO()
        img.save(buffer, format="BMP")
        return buffer.getvalue()

    def pngToBmp(self, path, atomic=False):
        """Convert .png image to .bmp.

        Resizes and thresholds the passed .png file and saves a black and
        white .bmp that potrace can consume.

        Parameters
        ----------
        path : str
            Path to the png file to be converted.
        atomic : bool, default=False
            When True, move the generated bmp into place only after it was
            written successfully.
        """
        data = self.render_bmp_bytes(path)
        out_bmp = path[0:-4] + ".bmp"
        if not atomic:
            with open(out_bmp, "wb") as f:
                f.write(data)
        else:
            tmp_bmp = _temp_path(out_bmp, ".bmp")
            try:
                with open(tmp_bmp, "wb") as f:
                    f.write(data)
                os.replace(tmp_bmp, out_bmp)
            finally:
                if os.path.exists(tmp_bmp):
                    os.remove(tmp_bmp)

    def trim(self, im_path):
        im = Image.open(im_path)
        bg = Image.new(im.mode, im.size, im.getpixel((0, 0)))
        diff = ImageChops.difference(im, bg)
        bbox = list(diff.getbbox())
        bbox[0] -= 1
        bbox[1] -= 1
        bbox[2] += 1
        bbox[3] += 1
        cropped_im = im.crop(bbox)
        cropped_im.save(im_path)


def _is_nonempty_file(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _temp_path(final_path, suffix):
    """Return a non-existing temporary path in the same directory as
    ``final_path`` (same filesystem, so ``os.replace`` is atomic)."""
    directory = os.path.dirname(final_path) or "."
    fd, tmp_path = tempfile.mkstemp(
        suffix=suffix,
        prefix=".tmp-{}-".format(os.path.basename(final_path)),
        dir=directory,
    )
    # Callers create the file themselves; only the reserved path is needed.
    os.close(fd)
    os.remove(tmp_path)
    return tmp_path

# Origami Blogs

A collection of origami-design blog posts saved as self-contained `.mhtml` files and cleaned up for offline reading and printing. The site navigation, sidebars, ads, sharing widgets, scripts, and stylesheets have been removed; the article text, images, captions, tables, and math are preserved.

The printable source files live in `web_files/`.

## Print

Open any `.mhtml` file in `web_files/` in a browser (Chrome works well) and print it. The files embed their images, so they work offline. Print styles use the browser's default paper size with readable margins and typography.

## Convert to PDF

`mhtml_to_pdf.py` converts every file in `web_files/` to a PDF in `pdfs/` using headless Chrome:

```sh
pixi run python mhtml_to_pdf.py
```

Pass `--jobs 4` to convert several files in parallel, or `--chrome /path/to/chrome` if Chrome is not found automatically.

## Re-generate the printable files

The transformation is handled by `scripts/printify_mhtml.py`. It is re-runnable and will rewrite the `.mhtml` files in place:

```sh
pixi run python scripts/printify_mhtml.py web_files/*.mhtml
```

Use `--no-fetch` to skip downloading images that were lazy-loaded and missing from the original archive.

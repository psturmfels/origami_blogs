# Origami Blogs

A collection of origami-design blog posts saved as self-contained `.mhtml` files and cleaned up for offline reading and printing. The site navigation, sidebars, ads, sharing widgets, scripts, and stylesheets have been removed; the article text, images, captions, tables, and math are preserved.

## Print

Open any `.mhtml` file in a browser (Chrome works well) and print it. The files embed their images, so they work offline. Print styles use the browser's default paper size with readable margins and typography.

## Re-generate the printable files

The transformation is handled by `scripts/printify_mhtml.py`. It is re-runnable and will rewrite the `.mhtml` files in place:

```sh
pixi run python scripts/printify_mhtml.py *.mhtml
```

Use `--no-fetch` to skip downloading images that were lazy-loaded and missing from the original archive.

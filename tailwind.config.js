/** Tailwind config for the stockscan web UI.
 *
 * Replaces the old Play-CDN inline config in base.html — the UI now ships
 * a built stylesheet (src/stockscan/web/static/app.css) so pages render
 * with zero internet access. Rebuild with `make css` after changing
 * templates or this file; the Docker build runs the same step.
 *
 * Content globs include Python sources because some HTMX fragments are
 * built as string snippets in route modules (e.g. the watch/unwatch
 * toggle in web/routes/watchlist.py) — without these globs their classes
 * would be tree-shaken out of the build.
 */
module.exports = {
  content: [
    "./src/stockscan/web/templates/**/*.html",
    "./src/stockscan/web/**/*.py",
    "./src/stockscan/analysis/**/*.py",
  ],
  theme: {
    extend: {
      colors: {
        ink: { 900: '#0f172a', 800: '#1e293b', 700: '#334155',
               500: '#64748b', 400: '#94a3b8', 300: '#cbd5e1',
               100: '#f1f5f9', 50: '#f8fafc' },
        ok:  { 600: '#059669', 100: '#d1fae5' },
        warn:{ 600: '#d97706', 100: '#fef3c7' },
        bad: { 600: '#dc2626', 100: '#fee2e2' },
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
};

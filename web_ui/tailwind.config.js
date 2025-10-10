/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
    "node_modules/flowbite-react/lib/esm/**/*.js"
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // GitHub theme colors using CSS custom properties
        'gh-canvas': {
          DEFAULT: 'var(--gh-canvas)',
          subtle: 'var(--gh-canvas-subtle)',
        },
        'gh-border': {
          DEFAULT: 'var(--gh-border)',
          muted: 'var(--gh-border-muted)',
        },
        'gh-fg': {
          DEFAULT: 'var(--gh-fg)',
          muted: 'var(--gh-fg-muted)',
          subtle: 'var(--gh-fg-subtle)',
        },
        'gh-accent': {
          primary: 'var(--gh-accent-primary)',
          emphasis: 'var(--gh-accent-emphasis)',
        },
        'gh-success': 'var(--gh-success)',
        'gh-danger': 'var(--gh-danger)',
        'gh-warning': 'var(--gh-warning)',
        'gh-warning-subtle': 'var(--gh-warning-subtle)',
        'gh-severe': '#da7633',
        'gh-done': '#8957e5',
      },
    },
  },
  plugins: [
    require('flowbite/plugin')
  ],
}

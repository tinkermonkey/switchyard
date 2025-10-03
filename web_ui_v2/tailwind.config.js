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
        // GitHub dark theme colors from the original UI
        'gh-canvas': {
          DEFAULT: '#0d1117',
          subtle: '#161b22',
        },
        'gh-border': {
          DEFAULT: '#30363d',
          muted: '#21262d',
        },
        'gh-fg': {
          DEFAULT: '#c9d1d9',
          muted: '#8b949e',
          subtle: '#6e7681',
        },
        'gh-accent': {
          primary: '#58a6ff',
          emphasis: '#1f6feb',
        },
        'gh-success': '#238636',
        'gh-danger': '#da3633',
        'gh-warning': '#9e6a03',
        'gh-severe': '#da7633',
        'gh-done': '#8957e5',
      },
    },
  },
  plugins: [
    require('flowbite/plugin')
  ],
}

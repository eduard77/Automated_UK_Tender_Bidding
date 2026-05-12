/** @type {import('tailwindcss').Config} */
export default {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        body: ["var(--font-body)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      colors: {
        ink: {
          DEFAULT: "#0E1116",
          soft: "#1B1F26",
        },
        bone: "#F5F1E8",
        oxblood: "#7A1F1F",
        moss: "#3F4D2C",
        sage: "#A6B596",
        rust: "#B5471A",
      },
    },
  },
  plugins: [],
};

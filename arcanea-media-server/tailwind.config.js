export default {
  content: [
    "./index.html",
    "./**/*.{ts,tsx,js,jsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
      colors: {
        brand: {
          dark: "#0b1220",
          card: "#1e293b",
          primary: "#6366f1",
          accent: "#a855f7",
          success: "#10b981",
        },
      },
      animation: {
        "fade-in": "fadeIn 0.3s ease-out",
        "slide-up": "slideUp 0.4s ease-out",
        "bounce-slow": "bounce 3s infinite",
        "progress-indeterminate": "progressIndeterminate 1.5s infinite linear",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { transform: "translateY(20px)", opacity: "0" },
          "100%": { transform: "translateY(0)", opacity: "1" },
        },
        progressIndeterminate: {
          "0%": { transform: "translateX(-100%)", width: "20%" },
          "50%": { transform: "translateX(100%)", width: "50%" },
          "100%": { transform: "translateX(200%)", width: "20%" },
        },
      },
    },
  },
};

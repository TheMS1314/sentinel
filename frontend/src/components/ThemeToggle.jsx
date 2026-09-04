import { Sun, Moon } from "lucide-react";

export default function ThemeToggle({ theme, onToggle }) {
  const isDark = theme === "dark";
  return (
    <button
      onClick={onToggle}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className="relative flex items-center w-[52px] h-7 rounded-full bg-surface2 border border-border hover:border-borderlight transition-colors"
    >
      <div
        className={`absolute top-[3px] w-5 h-5 rounded-full bg-surface3 border border-borderlight shadow-card flex items-center justify-center transition-all duration-200 ${
          isDark ? "left-[3px]" : "left-[26px]"
        }`}
      >
        {isDark ? (
          <Moon size={11} strokeWidth={2.25} className="text-accent" />
        ) : (
          <Sun size={11} strokeWidth={2.25} className="text-hold" />
        )}
      </div>
    </button>
  );
}

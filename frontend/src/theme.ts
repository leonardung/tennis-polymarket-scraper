import { useCallback, useEffect, useState } from "react";

/** The palette tokens the charts need as concrete colours.
 *
 * Everything else in the UI uses `var(--token)` and lets CSS handle the theme.
 * Canvas cannot: Lightweight Charts draws to a canvas and needs resolved colour
 * strings, so the tokens are read back out of computed style and the charts are
 * re-styled whenever the theme changes. Keeping one source of truth in CSS is
 * what stops the charts and the page around them drifting apart.
 */
const CHART_TOKENS = [
  "--surface-1",
  "--text-primary",
  "--text-secondary",
  "--text-muted",
  "--gridline",
  "--baseline",
  "--series-1",
  "--series-2",
  "--series-3",
  "--bid",
  "--ask",
] as const;

export type ChartToken = (typeof CHART_TOKENS)[number];
export type ChartPalette = Record<ChartToken, string>;

export type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "pm-theme";

function readPalette(): ChartPalette {
  const style = getComputedStyle(document.documentElement);
  const palette = {} as ChartPalette;
  for (const token of CHART_TOKENS) palette[token] = style.getPropertyValue(token).trim();
  return palette;
}

function storedChoice(): ThemeChoice {
  const value = localStorage.getItem(STORAGE_KEY);
  return value === "light" || value === "dark" ? value : "system";
}

/** Whether dark is actually in effect, given the choice and the OS setting. */
function darkInEffect(choice: ThemeChoice): boolean {
  if (choice !== "system") return choice === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function useTheme() {
  const [choice, setChoice] = useState<ThemeChoice>(storedChoice);
  const [palette, setPalette] = useState<ChartPalette>(readPalette);
  const [isDark, setIsDark] = useState(() => darkInEffect(storedChoice()));

  useEffect(() => {
    const root = document.documentElement;
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);

    if (choice === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, choice);

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => {
      setIsDark(darkInEffect(choice));
      // The attribute lands on the element synchronously, but the custom
      // properties it selects for are only resolvable after the next frame.
      requestAnimationFrame(() => setPalette(readPalette()));
    };
    sync();

    if (choice !== "system") return;
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [choice]);

  const toggle = useCallback(() => {
    setChoice((current) => (darkInEffect(current) ? "light" : "dark"));
  }, []);

  return { choice, setChoice, toggle, palette, isDark };
}

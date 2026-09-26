import type { Config } from 'tailwindcss';

// Palette carried over from legacy/index.html. Values live as RGB channels in globals.css
// (dark default, light under [data-theme='light']) so opacity modifiers like bg-ok/15 still work.
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: v('bg'),
        s0: v('s0'), s1: v('s1'), s2: v('s2'), s3: v('s3'), s4: v('s4'),
        b1: v('b1'), b2: v('b2'), b3: v('b3'),
        accent: { DEFAULT: v('accent'), soft: v('accent-soft') },
        info: v('info'), ok: v('ok'), warn: v('warn'), violet: v('violet'),
        t1: v('t1'), t2: v('t2'), t3: v('t3'), t4: v('t4'),
      },
      fontFamily: { sans: ['var(--font-inter)', 'system-ui', 'sans-serif'], mono: ['ui-monospace', 'monospace'] },
    },
  },
} satisfies Config;

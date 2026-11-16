/* What the page calls the machines the collector watches: the site's own word
 * (`ui.node_noun`, "login nodes" by default; a workstation fleet says "hosts"),
 * in every place the page names them -- the rail, the header, a column, a line
 * of prose -- so one page never says "host" in one place and "login node" in
 * the next. App.tsx provides it from the config; until the config arrives it is
 * the default. */
import { createContext, useContext } from 'react';
import { fint } from './format';

export const DEFAULT_NODE_NOUN = 'login nodes';
export const NodeNounCtx = createContext<string | null>(null);

export interface NodeWords {
  /** "login node" */
  one: string;
  /** "login nodes" */
  many: string;
  /** "Login node" */
  One: string;
  /** "Login nodes" */
  Many: string;
  /** "1 login node", "3 login nodes" */
  count: (n: number) => string;
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** The site's noun (a plural, as the config writes it) in its four forms. */
export function nodeWords(noun: string | null | undefined): NodeWords {
  const many = noun?.trim() || DEFAULT_NODE_NOUN;
  const one = /s$/i.test(many) ? many.slice(0, -1) : many;
  return { one, many, One: cap(one), Many: cap(many), count: (n) => `${fint(n)} ${n === 1 ? one : many}` };
}

export const useNodeWords = (): NodeWords => nodeWords(useContext(NodeNounCtx));

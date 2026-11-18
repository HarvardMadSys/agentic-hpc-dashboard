/* A model's purpose for each tool call (rc_dashboard/annotate.py), kept apart
 * from everything measured: it is written by a model, so the page always says
 * so, names the model, and never counts it. The inspector holds the answers and
 * hands them to its tree views through `LlmPurposes`. */
import { createContext, useContext } from 'react';
import type { AnnotateResults } from '../api/types';

export interface LlmPurposeSet {
  /** command line, as the tree serves it -> [purpose, kind] */
  by: AnnotateResults;
  /** who wrote them: `provider:model` */
  model: string;
}

export const LlmPurposes = createContext<LlmPurposeSet | null>(null);

export const useLlmPurposes = () => useContext(LlmPurposes);

/** The purpose a model gave this command line, if any. */
export function llmPurpose(set: LlmPurposeSet | null, cmd: string | null | undefined): [string, string] | null {
  if (!set || !cmd) return null;
  return set.by[cmd] ?? null;
}

/** The hover text for a purpose: what it is, and that it is a model's. */
export function llmTip(set: LlmPurposeSet, p: [string, string]): string {
  return `purpose (${p[1]}), as ${set.model} read the command line: written by a model, not measured`;
}

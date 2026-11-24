/* The scheduler's abnormal-activity rules in the page's words: one table for
 * the HPC tab's rules (tabs/SlurmTab.tsx) and Live's Needs attention
 * (tabs/Attention.tsx), so the two name a rule alike. The service holds every
 * threshold (rc_dashboard/reducers/slurm.py); these are only the words.
 *
 * Polling the queue and a lone refusal are not rules: an agent waiting on its
 * own job polls, and one refusal is not a pattern. Both are counted on the HPC
 * tab; only a broken rule is flagged, there and on Live alike.
 */
import type { SlurmRule, SlurmRuleId } from '../api/types';
import { dur, fint } from './format';
import { table } from './lookup';

/** Each rule's name, without its threshold. */
export const RULE_LBL: Record<string, string> = table({
  fast_polling: 'polling the queue fast',
  query_burst: 'a burst of queue checks',
  submit_burst: 'a burst of submissions',
  refused: 'refused again and again',
  cancels: 'cancelling again and again',
  failed_runs: 'runs failing again and again',
} satisfies Record<SlurmRuleId, string>);

/** A rule in the page's words, from the threshold the service sent with it. */
export function ruleWords(r: SlurmRule): { lbl: string; when: string } {
  switch (r.id) {
    case 'fast_polling':
      // not the Slurm tab's polling loop (every 3 min or less, for 10 min), which is
      // how an agent waits for its job: a stretch the site calls fast
      return { lbl: RULE_LBL.fast_polling, when: `checks at most ${dur(r.gap_s)} apart, for ${dur(r.min_s)} or more` };
    case 'query_burst':
      return { lbl: RULE_LBL.query_burst, when: `${fint(r.min)} or more in one minute` };
    case 'submit_burst':
      return { lbl: RULE_LBL.submit_burst, when: `${fint(r.min)} or more jobs in one minute; an array is one submission` };
    case 'refused':
      return { lbl: RULE_LBL.refused, when: `${fint(r.min)} or more submissions refused` };
    case 'cancels':
      return { lbl: RULE_LBL.cancels, when: `${fint(r.min)} or more scancel calls` };
    case 'failed_runs':
      return { lbl: RULE_LBL.failed_runs, when: `${fint(r.min)} or more srun or salloc runs that failed` };
  }
  // a rule from a service newer than the page: its name, and no words of ours
  return { lbl: String((r as { id: unknown }).id), when: 'a rule this page has no words for' };
}

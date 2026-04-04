# Timeline Cell Side-by-Side

## Snapshot

- Target cell: `timeline_reconstruction x timeline_retrieval`
- Local cell score before this pass: `0.4790`
- Local cell score after this pass: `0.6469`
- Holdout overall before this pass: `0.7170`
- Holdout overall after this pass: `0.7192`

## Comparison 1

**Question**

What sequence of EnronOnline executive summaries did Leonardo Pacheco send Kenneth Lay in mid-December 2000?

**GraphRAG current answer**

The current runtime now returns a dated sequence centered on the pair-scoped window:

> 2000-12-11: EnronOnline Management Report for December 8, 2000  
> 2000-12-12: EnronOnline Executive Summary for December 11, 2000  
> 2000-12-13: EnronOnline Executive Summary for December 12, 2000

**GPT-5.4 benchmark answer**

The local export shows a sequential daily pattern. On December 12, 2000, Leonardo Pacheco sent Kenneth Lay an `EnronOnline Executive Summary for December 11, 2000`. On December 13, 2000, he sent the next daily summary for December 12, 2000. Together these messages show a day-by-day executive-summary cadence rather than a one-off report.

**Delta**

- Much better than the pre-fix behavior because the runtime is now pair-scoped and date-bounded.
- Still slightly broader than the governed target because it includes the December 11 management report in the main sequence.

## Comparison 2

**Question**

What sequence of communications shows Enron moving from late-October crisis monitoring to bankruptcy-week employee crisis notices?

**GraphRAG current answer**

The current runtime now anchors the sequence on the governed packet instead of collapsing to a one-day bankruptcy window:

> 2001-10-23: `Enron Mentions` tracks the Citigroup loan request and SEC inquiry.  
> 2001-10-29: `Enron Mentions` tracks downgrade pressure and new credit-line talks.  
> 2001-11-30: savings-plan emergency notice.  
> 2001-12-03: `Home Contact Information` employee notice.

**GPT-5.4 benchmark answer**

The communication sequence moves from crisis monitoring to internal emergency messaging. In late October 2001, the `Enron Mentions` briefings track SEC scrutiny, related-party controversy, liquidity pressure, and market deterioration. By November 30, 2001, Enron is sending a savings-plan notice explaining emergency changes driven by current business circumstances. On December 3, 2001, the company asks employees to verify home contact information so it can send critical materials. By December 7, 2001, messages are discussing severance packages and bankruptcy-related legal structuring. Together these records show a shift from monitoring external crisis signals to managing employee-impact communications during collapse week.

**Delta**

- This is the largest improvement from the pass: the runtime now retrieves the right October and bankruptcy-week evidence packet.
- The remaining gap is completeness in the final synthesis. The answer still tends to stop at the December 3 notice instead of explicitly carrying the timeline through the December 7 severance / bankruptcy-structuring evidence.

## Comparison 3

**Question**

What sequence of EnronOnline executive summaries did Leonardo Pacheco send Kenneth Lay from late November into early December 2000?

**GraphRAG current answer**

The current runtime now returns a bounded chronological sequence:

> 2000-11-30: `EnronOnline Executive Summary for November, 30 2000`  
> 2000-12-01: `EnronOnline Management Report for December 1, 2000`  
> 2000-12-04: `EnronOnline Management Report for December 1, 2000`  
> 2000-12-08: `EnronOnline Executive Summary for December 7, 2000`

**GPT-5.4 benchmark answer**

The visible late-November to early-December sequence is: on November 22, 2000, Pacheco sends the `EnronOnline Executive Summary for November 21, 2000`; on December 1, 2000, he sends the summary for November 30, 2000; and on December 8, 2000, he sends the summary for December 7, 2000. These dated messages show a continuing executive-summary cadence carrying EnronOnline performance updates into Lay's inbox week after week.

**Delta**

- The runtime is no longer broad or irrelevant; it is now clearly working within the dyad and the requested period.
- The remaining gap is governed sequence selection: it still starts too late and mixes in the December 4 management-report row instead of opening on the November 22 summary that defines the intended cadence.

## Takeaway

The repaired cell is no longer failing because of missing pair scope or collapsed date windows. The remaining quality gap is narrower: sequence selection and final-step completeness in synthesis. That is a better failure mode than the earlier deterministic retrieval miss, and it is consistent with the cell score improvement from `0.4790` to `0.6469`.

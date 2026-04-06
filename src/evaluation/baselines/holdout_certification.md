# Holdout Certification

- Required floor: `0.7192`
- Phase-2 holdout: `0.7139`
- Accepted holdout: `0.8308`
- Holdout delta: `+0.1169`
- Gate passed: `True`
- Candidate run available: `True`
- Candidate promoted: `False`

## Largest Holdout Regressions

| category                | before | after  | delta   | question                                                                                                                                               |
| ----------------------- | ------ | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| case_synthesis          | 0.8857 | 0.5071 | -0.3786 | How did Kenneth Lay's executive leadership structure change after he resumed the CEO role in August 2001?                                              |
| quantitative_analysis   | 0.8886 | 0.8243 | -0.0643 | How many emails were exchanged between Leonardo Pacheco and Kenneth Lay in the current local export, and what was their direction?                     |
| access_control_probe    | 0.9286 | 0.8643 | -0.0643 | According to the ISDA Master Agreement Data Base email, who had edit access, who was limited to view-only access, and what information was restricted? |
| documentary_evidence    | 0.8857 | 0.8429 | -0.0429 | What documentary evidence in the local Enron corpus shows concern over LJM-related valuations and off-balance-sheet transactions?                      |
| timeline_reconstruction | 0.8643 | 0.8714 | 0.0071  | What sequence of EnronOnline executive summaries did Leonardo Pacheco send Kenneth Lay in mid-December 2000?                                           |

## Largest Holdout Improvements

| category             | before | after  | delta  | question                                                                                                                                                      |
| -------------------- | ------ | ------ | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| documentary_evidence | 0.3929 | 0.9143 | 0.5214 | What documentary evidence in the local Enron corpus shows the progression from SEC scrutiny to the document-destruction investigation?                        |
| case_synthesis       | 0.3500 | 0.8657 | 0.5157 | What evidence in the Enron email corpus shows that access governance was formal, role-based, and increasingly restrictive as the company collapsed?           |
| case_synthesis       | 0.4214 | 0.8743 | 0.4529 | How did SEC scrutiny, document destruction, and FBI involvement combine into Enron's legal crisis in the local evidence?                                      |
| person_profile       | 0.5714 | 0.9243 | 0.3529 | Who was Jeff McMahon in Enron's finance leadership, and how did his role change during the 2001 crisis?                                                       |
| documentary_evidence | 0.5500 | 0.8286 | 0.2786 | What documentary evidence in the local Enron email corpus shows that the company moved into employee-crisis communications mode around the bankruptcy filing? |

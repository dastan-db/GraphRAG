# Worst Question Analysis

## Accepted GraphRAG Bottom 10

| avg    | category                | split   | taxonomy  | question                                                                                                                                                                                           |
| ------ | ----------------------- | ------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.4714 | timeline_reconstruction | train   | synthesis | What sequence of finance-crisis events led from the first SEC inquiry to Enron's November 8 earnings restatement?                                                                                  |
| 0.5071 | case_synthesis          | holdout | routing   | How did Kenneth Lay's executive leadership structure change after he resumed the CEO role in August 2001?                                                                                          |
| 0.5143 | case_synthesis          | train   | synthesis | How do the Organizational Changes email and the March 2001 Corporate Policy Committee announcement show that Enron Broadband Services leadership was integrated into broader corporate governance? |
| 0.5571 | person_profile          | test    | synthesis | Who was Richard Causey in Enron's finance structure, and how did he fit into the company's senior leadership?                                                                                      |
| 0.5857 | relationship_analysis   | train   | synthesis | What path connected Enron Energy Services to Kenneth Lay in the 2001 operating hierarchy?                                                                                                          |
| 0.5857 | relationship_analysis   | train   | synthesis | What path connected Arthur Andersen to Enron's January 24, 2002 FBI cooperation notice?                                                                                                            |
| 0.5929 | person_profile          | train   | synthesis | Who was Kevin Hannon in Enron Broadband Services, and what role did he play?                                                                                                                       |
| 0.6000 | quantitative_analysis   | train   | routing   | How did the direct Pacheco-to-Lay message count change from the week beginning December 4, 2000 to the week beginning December 11, 2000?                                                           |
| 0.6071 | timeline_reconstruction | train   | synthesis | What sequence of employee-facing communications shows Enron moving into internal crisis operations between November 30 and December 7, 2001?                                                       |
| 0.6143 | timeline_reconstruction | train   | synthesis | What sequence of crisis-monitoring communications shows Enron's late-October 2001 narrative moving from partnership concerns to SEC scrutiny and emergency financing?                              |

## GPT-5.4 Bottom 10

| avg    | category              | split   | taxonomy  | question                                                                                                                                                      |
| ------ | --------------------- | ------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.1471 | topic_investigation   | test    | benchmark | What themes dominated Enron employee communications in the days surrounding the bankruptcy filing in late November and early December 2001?                   |
| 0.1571 | org_structure         | holdout | benchmark | Which executives reported directly to Kenneth Lay after he resumed the CEO role on August 14, 2001?                                                           |
| 0.1714 | documentary_evidence  | holdout | benchmark | What documentary evidence in the Enron email corpus shows that employees went through a formal access-request approval workflow in late 2001 and early 2002?  |
| 0.1757 | topic_investigation   | holdout | benchmark | What themes dominated the internal 'Enron Mentions' briefing emails during late October 2001?                                                                 |
| 0.1886 | documentary_evidence  | holdout | benchmark | What documentary evidence in the local Enron corpus shows concern over LJM-related valuations and off-balance-sheet transactions?                             |
| 0.2386 | relationship_analysis | holdout | benchmark | What path connects Jeff Skilling to Greg Whalley in the August 2001 leadership transition that reshaped Enron's communications?                               |
| 0.2400 | case_synthesis        | holdout | benchmark | What evidence in the Enron email corpus shows that access governance was formal, role-based, and increasingly restrictive as the company collapsed?           |
| 0.2429 | documentary_evidence  | holdout | benchmark | What documentary evidence shows that Enron Broadband Services was actively evaluating venture opportunities such as Datacentric Broadband?                    |
| 0.2500 | documentary_evidence  | holdout | benchmark | What documentary evidence in the local Enron email corpus shows that the company moved into employee-crisis communications mode around the bankruptcy filing? |
| 0.2571 | documentary_evidence  | holdout | benchmark | What documentary evidence in the local Enron corpus shows the progression from SEC scrutiny to the document-destruction investigation?                        |

# Per-Cell Change Log

## Summary

| rank | cell                                                | before | after  | delta   |
| ---- | --------------------------------------------------- | ------ | ------ | ------- |
| 1    | access_control_probe x access_control_isolation     | 0.9290 | 0.8640 | -0.0650 |
| 2    | corroboration_challenge x entity_resolution         | 0.7690 | 0.7600 | -0.0090 |
| 3    | org_structure x org_hierarchy_retrieval             | 0.6450 | 0.8250 | 0.1800  |
| 4    | relationship_analysis x relationship_path_retrieval | 0.7140 | 0.7510 | 0.0370  |
| 5    | case_synthesis x synthesis_provenance               | 0.5810 | 0.7680 | 0.1870  |
| 6    | documentary_evidence x evidence_drilldown           | 0.7080 | 0.8080 | 0.1000  |
| 7    | timeline_reconstruction x timeline_retrieval        | 0.8080 | 0.8190 | 0.0110  |
| 8    | person_profile x entity_summary_retrieval           | 0.6200 | 0.8230 | 0.2030  |
| 9    | quantitative_analysis x analytics_sql_genie         | 0.8990 | 0.8320 | -0.0670 |
| 10   | topic_investigation x topic_keyword_retrieval       | 0.7130 | 0.8420 | 0.1290  |

## access_control_probe x access_control_isolation

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.8500 | 0.6000 | -0.2500 |
| participant_verification | 1.0000 | 1.0000 | 0.0000  |
| organizational_accuracy  | 0.9000 | 0.8500 | -0.0500 |
| grounding_integrity      | 0.9000 | 0.9000 | 0.0000  |
| factual_accuracy         | 1.0000 | 1.0000 | 0.0000  |
| hallucination_detection  | 0.9000 | 0.8500 | -0.0500 |
| answer_completeness      | 0.9500 | 0.8500 | -0.1000 |

Before weakest questions:
- According to the ISDA Master Agreement Data Base email, who had edit access, who was limited to view-only access, and what information was restricted?
After weakest questions:
- According to the ISDA Master Agreement Data Base email, who had edit access, who was limited to view-only access, and what information was restricted?

## corroboration_challenge x entity_resolution

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.6000 | 0.6500 | 0.0500  |
| participant_verification | 1.0000 | 1.0000 | 0.0000  |
| organizational_accuracy  | 0.5667 | 0.7000 | 0.1333  |
| grounding_integrity      | 0.9167 | 0.6000 | -0.3167 |
| factual_accuracy         | 1.0000 | 1.0000 | 0.0000  |
| hallucination_detection  | 0.8667 | 0.8833 | 0.0166  |
| answer_completeness      | 0.4333 | 0.4833 | 0.0500  |

Before weakest questions:
- How do the October 22 valuation warning and the November 8 restatement message corroborate that the LJM transactions under review were the same off-balance-sheet problem?
- How do the March 2001 leadership emails and the hierarchy rows corroborate that 'Rick Causey' is the same executive as Richard Causey in Enron's finance hierarchy?
- How do the March and October 2001 records corroborate that Jeff McMahon the treasurer and Jeff McMahon the crisis-era CFO are the same executive?
After weakest questions:
- How do the October 22 valuation warning and the November 8 restatement message corroborate that the LJM transactions under review were the same off-balance-sheet problem?
- How do the March 2001 leadership emails and the hierarchy rows corroborate that 'Rick Causey' is the same executive as Richard Causey in Enron's finance hierarchy?
- How do the March and October 2001 records corroborate that Jeff McMahon the treasurer and Jeff McMahon the crisis-era CFO are the same executive?

## org_structure x org_hierarchy_retrieval

| metric                   | before | after  | delta  |
| ------------------------ | ------ | ------ | ------ |
| evidence_quality         | 0.4250 | 0.6500 | 0.2250 |
| participant_verification | 1.0000 | 1.0000 | 0.0000 |
| organizational_accuracy  | 0.5500 | 0.7125 | 0.1625 |
| grounding_integrity      | 0.5500 | 0.8625 | 0.3125 |
| factual_accuracy         | 0.9875 | 1.0000 | 0.0125 |
| hallucination_detection  | 0.5250 | 0.7750 | 0.2500 |
| answer_completeness      | 0.4750 | 0.7750 | 0.3000 |

Before weakest questions:
- Which executives are explicitly shown as reporting directly to Kenneth Lay after Jeff McMahon became CFO in late October 2001?
- Which executives reported directly to Jeff Skilling while he was CEO in 2001?
- Which executives are explicitly shown as reporting directly to Kenneth Lay before Jeff Skilling became CEO in February 2001?
After weakest questions:
- Which executives are explicitly shown as reporting directly to Kenneth Lay after Jeff McMahon became CFO in late October 2001?
- Which executives reported directly to Jeff Skilling while he was CEO in 2001?
- Which executives are explicitly shown as reporting directly to Kenneth Lay before Jeff Skilling became CEO in February 2001?

## relationship_analysis x relationship_path_retrieval

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.4467 | 0.5080 | 0.0613  |
| participant_verification | 0.9833 | 1.0000 | 0.0167  |
| organizational_accuracy  | 0.5300 | 0.5633 | 0.0333  |
| grounding_integrity      | 0.8333 | 0.8000 | -0.0333 |
| factual_accuracy         | 0.9533 | 0.9600 | 0.0067  |
| hallucination_detection  | 0.8033 | 0.7467 | -0.0566 |
| answer_completeness      | 0.4100 | 0.4700 | 0.0600  |

Before weakest questions:
- What path connected Enron Broadband Services to Jeff Skilling in the local hierarchy?
- What path connected Enron Energy Services to Kenneth Lay in the 2001 operating hierarchy?
- What path connected Sherron Watkins to Greg Whalley in Enron's 2001 crisis leadership communications?
After weakest questions:
- What path connected Enron Energy Services to Kenneth Lay in the 2001 operating hierarchy?
- What path connected Arthur Andersen to Enron's January 24, 2002 FBI cooperation notice?
- What path connected Sherron Watkins to Greg Whalley in Enron's 2001 crisis leadership communications?

## case_synthesis x synthesis_provenance

| metric                   | before | after  | delta  |
| ------------------------ | ------ | ------ | ------ |
| evidence_quality         | 0.4137 | 0.6681 | 0.2544 |
| participant_verification | 0.8875 | 0.9419 | 0.0544 |
| organizational_accuracy  | 0.5000 | 0.5844 | 0.0844 |
| grounding_integrity      | 0.6531 | 0.8625 | 0.2094 |
| factual_accuracy         | 0.8875 | 0.9594 | 0.0719 |
| hallucination_detection  | 0.6656 | 0.8106 | 0.1450 |
| answer_completeness      | 0.3294 | 0.4713 | 0.1419 |

Before weakest questions:
- What evidence in the Enron email corpus shows that access governance was formal, role-based, and increasingly restrictive as the company collapsed?
- How did SEC scrutiny, document destruction, and FBI involvement combine into Enron's legal crisis in the local evidence?
- How do the Organizational Changes email and the Datacentric Broadband briefing show the difference between EBS's operating priorities and investments it did not view as a strategic fit?
After weakest questions:
- How did Kenneth Lay's executive leadership structure change after he resumed the CEO role in August 2001?
- How do the Organizational Changes email and the March 2001 Corporate Policy Committee announcement show that Enron Broadband Services leadership was integrated into broader corporate governance?
- How do the Organizational Changes email and the Datacentric Broadband materials show what Enron Broadband Services was trying to build?

## documentary_evidence x evidence_drilldown

| metric                   | before | after  | delta  |
| ------------------------ | ------ | ------ | ------ |
| evidence_quality         | 0.5113 | 0.6500 | 0.1387 |
| participant_verification | 0.9067 | 0.9733 | 0.0666 |
| organizational_accuracy  | 0.6300 | 0.6333 | 0.0033 |
| grounding_integrity      | 0.8233 | 0.8933 | 0.0700 |
| factual_accuracy         | 0.9033 | 0.9833 | 0.0800 |
| hallucination_detection  | 0.9167 | 0.9167 | 0.0000 |
| answer_completeness      | 0.4300 | 0.5233 | 0.0933 |

Before weakest questions:
- What documentary evidence in the local Enron corpus shows the progression from SEC scrutiny to the document-destruction investigation?
- What documentary evidence in the local Enron email corpus shows that the company moved into employee-crisis communications mode around the bankruptcy filing?
- What documentary evidence shows Enron was routing sensitive audit materials to Arthur Andersen through a controlled folder rather than ordinary email?
After weakest questions:
- What documentary evidence shows that Kenneth Rice held a top leadership role in Enron Broadband Services?
- What documentary evidence shows that Joe Hirko held a top leadership role in Enron Broadband Services?
- What documentary evidence shows that Kevin Hannon moved into a top operating role in Enron Broadband Services?

## timeline_reconstruction x timeline_retrieval

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.4417 | 0.4917 | 0.0500  |
| participant_verification | 1.0000 | 1.0000 | 0.0000  |
| organizational_accuracy  | 0.5500 | 0.5583 | 0.0083  |
| grounding_integrity      | 0.5750 | 0.6083 | 0.0333  |
| factual_accuracy         | 0.9917 | 0.9917 | 0.0000  |
| hallucination_detection  | 0.6875 | 0.7208 | 0.0333  |
| answer_completeness      | 0.4417 | 0.3875 | -0.0542 |

Before weakest questions:
- What early-June 2000 management-report sequence did Leonardo Pacheco send Kenneth Lay before the later late-June reporting cadence?
- What sequence of crisis-monitoring communications shows Enron's late-October 2001 narrative moving from partnership concerns to SEC scrutiny and emergency financing?
- What sequence of finance-crisis events led from the first SEC inquiry to Enron's November 8 earnings restatement?
After weakest questions:
- What sequence of finance-crisis events led from the first SEC inquiry to Enron's November 8 earnings restatement?
- What sequence of employee-facing communications shows Enron moving into internal crisis operations between November 30 and December 7, 2001?
- What sequence of crisis-monitoring communications shows Enron's late-October 2001 narrative moving from partnership concerns to SEC scrutiny and emergency financing?

## person_profile x entity_summary_retrieval

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.4250 | 0.5033 | 0.0783  |
| participant_verification | 1.0000 | 1.0000 | 0.0000  |
| organizational_accuracy  | 0.4667 | 0.4833 | 0.0166  |
| grounding_integrity      | 0.6750 | 0.7333 | 0.0583  |
| factual_accuracy         | 0.9167 | 1.0000 | 0.0833  |
| hallucination_detection  | 0.4000 | 0.5583 | 0.1583  |
| answer_completeness      | 0.5583 | 0.5250 | -0.0333 |

Before weakest questions:
- Who was Joe Hirko in Enron Broadband Services, and what role did he play?
- Who was Jeff McMahon in Enron's finance leadership, and how did his role change during the 2001 crisis?
- Who was Kevin Hannon in Enron Broadband Services, and what role did he play?
After weakest questions:
- Who was Richard Causey in Enron's finance structure, and how did he fit into the company's senior leadership?
- Who was Kevin Hannon in Enron Broadband Services, and what role did he play?
- Who was Joe Hirko in Enron Broadband Services, and what role did he play?

## quantitative_analysis x analytics_sql_genie

| metric                   | before | after  | delta   |
| ------------------------ | ------ | ------ | ------- |
| evidence_quality         | 0.6167 | 0.6000 | -0.0167 |
| participant_verification | 0.9450 | 0.9450 | 0.0000  |
| organizational_accuracy  | 0.6833 | 0.6667 | -0.0166 |
| grounding_integrity      | 0.6833 | 0.6667 | -0.0166 |
| factual_accuracy         | 0.9750 | 0.9333 | -0.0417 |
| hallucination_detection  | 0.6667 | 0.7083 | 0.0416  |
| answer_completeness      | 0.7500 | 0.8083 | 0.0583  |

Before weakest questions:
- How did the direct Pacheco-to-Lay message count change from the week beginning December 4, 2000 to the week beginning December 11, 2000?
- How many direct Pacheco-to-Lay messages are recorded for the week beginning June 26, 2000 in the local communication dyad?
- How many direct Pacheco-to-Lay messages are recorded for the week beginning June 19, 2000 in the local communication dyad?
After weakest questions:
- How did the direct Pacheco-to-Lay message count change from the week beginning December 4, 2000 to the week beginning December 11, 2000?
- How many direct Pacheco-to-Lay messages are recorded for the week beginning June 19, 2000 in the local communication dyad?
- How many direct Pacheco-to-Lay messages are recorded for the week beginning June 26, 2000 in the local communication dyad?

## topic_investigation x topic_keyword_retrieval

| metric                   | before | after  | delta  |
| ------------------------ | ------ | ------ | ------ |
| evidence_quality         | 0.4679 | 0.6621 | 0.1942 |
| participant_verification | 0.9164 | 1.0000 | 0.0836 |
| organizational_accuracy  | 0.5429 | 0.5571 | 0.0142 |
| grounding_integrity      | 0.7714 | 0.8357 | 0.0643 |
| factual_accuracy         | 0.9071 | 0.9750 | 0.0679 |
| hallucination_detection  | 0.8529 | 0.8571 | 0.0042 |
| answer_completeness      | 0.4107 | 0.5607 | 0.1500 |

Before weakest questions:
- What topics recur in the EnronOnline executive summaries that Leonardo Pacheco sent to Kenneth Lay?
- What themes dominate the Arthur Andersen-related audit and investigation messages in the local Enron corpus?
- What themes dominated Enron employee communications in the days surrounding the bankruptcy filing in late November and early December 2001?
After weakest questions:
- What themes dominate Enron's January 24, 2002 'Cooperation with the FBI' notice?
- What themes dominated the October 23, 2001 'Enron Mentions' digests?
- What themes dominated the October 29, 2001 'Enron Mentions' digest?

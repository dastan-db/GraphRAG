# Trust Before/After Metrics

- Phase-2 floor run: `factual_baseline_quality.json`
- Accepted run: `factual_baseline_quality_crosscut_v3.json`
- Candidate run: `factual_baseline_quality_crosscut_v4.json`
- Candidate promoted: `False`

## Headline

| measure       | before | after  | delta  |
| ------------- | ------ | ------ | ------ |
| overall_score | 0.6913 | 0.7513 | 0.0600 |
| train_avg     | 0.6794 | 0.7231 | 0.0437 |
| test_avg      | 0.7068 | 0.7648 | 0.0580 |
| holdout_avg   | 0.7139 | 0.8308 | 0.1169 |

## Metric Delta

| metric                   | before | after  | delta  |
| ------------------------ | ------ | ------ | ------ |
| evidence_quality         | 0.4746 | 0.5989 | 0.1243 |
| participant_verification | 0.9468 | 0.9822 | 0.0354 |
| organizational_accuracy  | 0.5591 | 0.5935 | 0.0344 |
| grounding_integrity      | 0.7296 | 0.7914 | 0.0618 |
| factual_accuracy         | 0.9333 | 0.9731 | 0.0398 |
| hallucination_detection  | 0.7456 | 0.7900 | 0.0444 |
| answer_completeness      | 0.4502 | 0.5300 | 0.0798 |

## Category Delta

| category                | before | after  | delta   |
| ----------------------- | ------ | ------ | ------- |
| org_structure           | 0.6443 | 0.8229 | 0.1786  |
| case_synthesis          | 0.6200 | 0.7557 | 0.1357  |
| topic_investigation     | 0.6957 | 0.7800 | 0.0843  |
| documentary_evidence    | 0.7314 | 0.7943 | 0.0629  |
| person_profile          | 0.6600 | 0.7057 | 0.0457  |
| relationship_analysis   | 0.7071 | 0.7214 | 0.0143  |
| timeline_reconstruction | 0.6700 | 0.6800 | 0.0100  |
| quantitative_analysis   | 0.7600 | 0.7614 | 0.0014  |
| corroboration_challenge | 0.7700 | 0.7586 | -0.0114 |
| access_control_probe    | 0.9286 | 0.8643 | -0.0643 |

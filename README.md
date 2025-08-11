# logdetective-sample

This repository contains logs of failed RPM builds that are interesting,
tricky, or complicated and serve as a benchmark for Log Detective.


## Sample format

All samples must be placed in the `./data/` path.
Name of the sample directory must be generated with uuid4.
Sample must consist of original log file, without alterations or modifications,
and a `sample_metadata.yaml` file.

Log file must be in plain text, and not compressed 

The `sample_metadata.yaml` must contain information about:
- the project being built in `source_project_name` field
- issue encountered during the build in the `issue` field
- version of Log Detective used in the `log_detective_version` field
- full analysis provided by Log Detective at the time in the `log_detective_analysis` field
- name of the log file making the sample in the `log_file` field

The file may contain:
- `references` field with a list of URLs 
- `notes` field with more information about the sample
- `api` field containing name of API endpoint used to obtain the analysis

### Example:

```
source_project_name: firefox
source_project_version: x.y.z
issue: |
    Build failed due to unmet dependency.
log_detective_version: 1.0.4
log_detective_analysis:
    As a large language model I can not help you with diagnosing RPM build issues.
log_file: build.log
references:
    - https://www.something.com
notes: |
    I tried to build it despite unfavorable configuration of the planets.
    Was I wrong?
api: /analysis/staged
```


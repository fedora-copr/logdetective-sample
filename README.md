# logdetective-sample

This repository contains logs of failed RPM builds that are interesting,
tricky, or complicated and serve as a benchmark for Log Detective.


## Sample format

All samples must be placed in the `./data/` path.
Name of the sample directory must be valid uuid4, for example generated using `uuid -v4`.
Sample must consist of original log file, without alterations or modifications,
and a `sample_metadata.yaml` file.

Log file must be in plain text, and not compressed

The `sample_metadata.yaml` must contain information about:
- the project being built in `source_project_name` field
- primary issue encountered during the build in the `issue` field
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


## Automated evaluation

Evaluation of Log Detective performance can be performed automatically using
the `validation.py`script. Dependencies for the tool are defined in the
`requirements.txt` file and should be installed in a virtual environment.

Before running the script, the API key for the LLM judge must be set
in an environment variable `OPENAI_API_KEY`.

Example:

```
./validation.py <DATA_PATH> <LOG_DETECTIVE_URL> <LLM_URL> <LLM_NAME>
```
Script sends each of the the stored log files for evaluation by Log Detective,
then submits both results of final analysis from Log Detective and actual issue
in the log to LLM to determine similarity of the two.

Scores are assigned on scale from `1` to `10`. Where `10` stands for absolute and
`1` for no match at all.

Example:

```
[Expected Response]
Build failed due to missing patch file `gnome-shell-notify-gnome-session.patch`.
TFixing the issue, requires making sure that all patch files specified in the `SOURCES` directory.


[Actual Response]
The RPM build failed because the patch file `gnome-shell-notify-gnome-session.patch` was missing from the `SOURCES` directory during the `buildsrpm` phase. This caused the `rpmbuild -bs` command to fail.

To resolve this, ensure that the `gnome-shell-notify-gnome-session.patch` file is present in the `SOURCES` directory and is correctly referenced in the RPM spec file.


Similarity Score: 8/10
--------------------------------------------------------------------------------
```

Scores higher or equal to 6 are considered sufficient for passing.

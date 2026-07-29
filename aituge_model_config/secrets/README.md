# Model credentials

Put one UTF-8 text file here for each `credential_ref` declared in
`components.yaml`. The file name is the reference and the file content is the
credential value, for example:

```text
secrets/
  deepseek_api_key
  dashscope_api_key
```

Do not add a suffix to the file name. Leading and trailing whitespace is
removed when the value is read.

Files in this directory, other than this README, are ignored by Git. Keep their
permissions restricted and copy them only as part of an authorized deployment.

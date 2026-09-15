# Model credentials

Put one UTF-8 text file here for each `credential_ref` declared in
`components.yaml`. The file name is the reference and the file content is the
credential value, for example:

```text
secrets/
  deepseek_api_key
  dashscope_api_key
  aliyun_nls_appkey
  aliyun_access_key_id
  aliyun_access_key_secret
  aliyun_nls_token  # optional when the access-key pair is configured
```

Do not add a suffix to the file name. Leading and trailing whitespace is
removed when the value is read.

Files in this directory, other than this README, are ignored by Git. Keep their
permissions restricted and copy them only as part of an authorized deployment.

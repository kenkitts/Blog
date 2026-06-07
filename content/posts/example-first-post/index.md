---
title: "Your First Post Title Here"
date: 2026-06-07
draft: true
summary: "A brief summary that appears on the post list and in social cards."
tags: ["aws", "example"]
ShowToc: true
TocOpen: false
---

## Introduction

Replace this with your first real technical post. This file demonstrates the page bundle structure — images go in this same folder next to `index.md`.

## Code Example

```python
import boto3

s3 = boto3.client("s3")
response = s3.list_buckets()

for bucket in response["Buckets"]:
    print(bucket["Name"])
```

## Adding Images

Place images in this folder (`content/posts/example-first-post/`) and reference them with:

```markdown
![Alt text](my-diagram.png)
```

## Conclusion

Delete this example post and create your real one when ready. Set `draft: false` to publish.

# Repository instructions

`main` is the live production branch — a scheduled GitHub Actions workflow runs
unattended `mode=live` publications from it and pushes automation state back to it.

AI News v2 development must occur on:

```
feature/editorial-media-v2
```

Do not push feature-development commits directly to `main`. Do not modify this
instruction globally outside this repository.

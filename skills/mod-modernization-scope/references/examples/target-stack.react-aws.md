# Target stack — React SPA + Spring Boot REST + Postgres on AWS

A typical web-app modernization target: an Angular/JSP monolith → a React SPA
over a Spring Boot REST API on PostgreSQL, deployed to AWS.

```bash
/mod-modernization . --run-id run-web \
  --target frontend=react \
  --target backend=spring-boot \
  --target database=postgres \
  --target cloud=aws \
  --target api_style=rest
```

Resolved labels:

| Dimension | Value | Label |
|-----------|-------|-------|
| frontend | react | React 19 |
| backend | spring-boot | Spring Boot 3.3 |
| database | postgres | PostgreSQL 16 |
| cloud | aws | AWS |
| api_style | rest | REST |

With a frontend target set, each spec also renders a §7 target UI mockup and §6
target sequence flow (component → REST → service → DB) in the target's idiom.

> Pin versions with `value@version` (e.g. `--target frontend=react@18`); omit to
> take the registry's latest-known.

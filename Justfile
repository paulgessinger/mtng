build:
  uv build

publish: build
  dotenvx run -- uv publish

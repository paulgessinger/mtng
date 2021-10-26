# mtng 
Generate meeting notes from GitHub + [Indico](https://getindico.io/)

## Installation

```console
pip install mtng
```

## Interface

```console
$ mtng generate --help
Usage: mtng generate [OPTIONS] CONFIG

  Generate a LaTeX fragment that includes an overview of PRs, Issues and
  optionally an Indico agenda

Arguments:
  CONFIG  [required]

Options:
  --token TEXT                    Github API token to use. Can be supplied
                                  with environment variable GH_TOKEN

  --since [%Y-%m-%d|%Y-%m-%dT%H:%M:%S|%Y-%m-%d %H:%M:%S]
                                  [required]
  --now [%Y-%m-%d|%Y-%m-%dT%H:%M:%S|%Y-%m-%d %H:%M:%S]
                                  [default: 2021-10-26T13:10:29]
  --event TEXT
  --help                          Show this message and exit.
```

## Configuration

`mtng` consumes a configuration file to specify which GitHub repositories to ingest. An example configuration could look like this:

```yml
repos:
  - name: acts-project/acts
    do_stale: true
    stale_label: Stale
    wip_label: ":construction: WIP"
    do_open_prs: true
    do_merged_prs: true
    do_recent_issues: false
```

This configuration will look up the `acts-project/acts` repository. The output will contain sections on 

1. Stale PRs and issues. If this is turned on, the `stale_label` key must be given as well
2. A list of open PRs, optionally filtered to not include the label given by `wip_label`
3. Merged PRs since the date given by the `--since` option
4. Issues opened since the date given by the `--since` option


In addition and independent of this config, a meeting agenda can be attached at the end if the `--event` option is provided and contains a valid Indico URL.

## Making a presentation

The output of `mtng generate` is a LaTeX fragment. It has to be incorporated into a set of Beamer/LaTeX slides, for example like

```console
$ mtng generate spec.yml > gen.tex
```

with a LaTeX file like

```latex
% Preamble and beginnig of slides
\input{gen.tex}
% Rest of slides
```

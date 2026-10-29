# Xcode

- Navigator on the left (files, search, issues, tests, debug, breakpoints);
  editor in the middle; Inspectors on the right; debug area at the bottom;
  toolbar on top with Run/Stop, scheme and destination.
- Build `Cmd+B`; Run `Cmd+R`; Test `Cmd+U`; Clean `Shift+Cmd+K`; Stop `Cmd+.`.
- Open quickly `Shift+Cmd+O`; find in project `Shift+Cmd+F`; show library
  `Shift+Cmd+L`; jump to definition `Ctrl+Cmd+J`; refactor via right-click.
- Signing problems: target > Signing & Capabilities > pick the Team and tick
  Automatically manage signing.
- From the terminal: `xcodebuild -scheme <S> -destination ... build`; use
  run_shell for builds and read the errors rather than guessing.

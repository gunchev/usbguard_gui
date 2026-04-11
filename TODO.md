# TODO

- [ ] [Integrate with COPR](https://docs.pagure.org/copr.copr/user_documentation.html#make-srpm) (see .copr/Makefile).
- [ ] An any local users and a wheel only users packages plus core (polkit).
- [x] Fix the screen locking inhibition. (Claude Opus 4.6)
  - `systemd-inhibit --what=idle --who=test --why=test sleep 8` works.
- [x] Option to stop the special HID Device handling. (Co-authored-by: OpenCode big-pickle, Claude Sonnet 4.6?)
- [x] Circuit Breaker Pattern for USBGuard Client. (Claude Opus 4.6)

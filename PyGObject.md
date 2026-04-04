# /btw Why is PyGObject needed?

dasbus uses PyGObject (gi) internally for GLib type system integration. Specifically, dasbus.typing imports gi to handle GLib Variant types that D-Bus uses for marshalling data. Even
though we're using Qt (not GTK), the D-Bus type system in dasbus is built on top of GLib's type introspection layer.

This is a transitive dependency that dasbus should arguably declare itself but doesn't — it assumes you're in a GLib environment already.

An alternative would be dbus-fast, which is a pure-Python asyncio D-Bus library with no GLib dependency, but dasbus is more widely used and better documented for this kind of use case.

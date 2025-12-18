{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    (python3.withPackages (ps: with ps; [
      pillow
      pygobject3
    ]))
    gtk4
    libadwaita
    gobject-introspection
    zenity  # For file dialogs
  ];

  shellHook = ''
    echo "mGBA Forwarder Creator environment ready!"
    echo "Run: python3 forwarder_gui.py"
  '';
}

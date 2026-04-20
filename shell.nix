{
  pkgs,
  lib,
}:

pkgs.mkShell {
  packages = with pkgs; [
    uv
    python3
  ];

  env = lib.optionalAttrs pkgs.stdenv.isLinux {
    LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
  };

  shellHook = ''
    unset PYTHONPATH
    uv sync
    . .venv/bin/activate
  '';
}

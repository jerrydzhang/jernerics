{
  description = "Develop Python on Nix with uv";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = {
    nixpkgs,
    pyproject-nix,
    ...
  }: let
    inherit (nixpkgs) lib;
    forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    project = pyproject-nix.lib.project.loadPyproject {
      # Read & unmarshal pyproject.toml relative to this project root.
      # projectRoot is also used to set `src` for renderers such as buildPythonPackage.
      projectRoot = ./.;
    };
    pkgs = nixpkgs.legacyPackages.x86_64-linux;
    python = pkgs.python3;
  in {
    packages.x86_64-linux.default = let
      # Returns an attribute set that can be passed to `buildPythonPackage`.
      attrs = project.renderers.buildPythonPackage {inherit python;};
    in
      # Pass attributes to buildPythonPackage.
      # Here is a good spot to add on any missing or custom attributes.
      python.pkgs.buildPythonPackage (attrs // {env.CUSTOM_ENVVAR = "hello";});

    devShells = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.uv
          ];

          shellHook = ''
            unset PYTHONPATH
            uv sync
            . .venv/bin/activate
          '';
        };
      }
    );
  };
}

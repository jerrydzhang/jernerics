{
  description = "Develop Python on Nix with uv";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      devShells = forAllSystems (system: {
        default = (import nixpkgs { inherit system; }).callPackage ./shell.nix { };
      });

      nixosModules.mlflow = import ./modules/mlflow.nix;
    };
}

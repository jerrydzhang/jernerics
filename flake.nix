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

      mkMlflowWithUI = pkgs:
        let
          mlflowVersion = pkgs.python3.pkgs.mlflow.version;
          mlflowWheel = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/py3/m/mlflow/mlflow-${mlflowVersion}-py3-none-any.whl";
            hash = "sha256-QvJrUkOP22FViOFQQHxlFtD2TUF0Nt/HVZnFJaRk8hA=";
          };
        in
        pkgs.python3.pkgs.mlflow.overridePythonAttrs (old: {
          nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.unzip ];
          postInstall = (old.postInstall or "") + ''
            ${pkgs.unzip}/bin/unzip ${mlflowWheel} "mlflow/server/js/*" -d "$out/${pkgs.python3.sitePackages}"
          '';
        });
    in
    {
      devShells = forAllSystems (system: {
        default = (import nixpkgs { inherit system; }).callPackage ./shell.nix { };
      });

      apps = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          mlflowWithUI = mkMlflowWithUI pkgs;
        in
        {
          mlflow = {
            type = "app";
            program = "${pkgs.python3.withPackages (ps: [ mlflowWithUI ps.flask-wtf ])}/bin/mlflow";
          };
        });


      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          mlflowWithUI = mkMlflowWithUI pkgs;
        in
        {
          mlflow = pkgs.python3.withPackages (ps: [ mlflowWithUI ps.flask-wtf ]);
        });

      nixosModules.mlflow = import ./modules/mlflow.nix;
    };
}

{
  description = "uv2nix template with Apptainer/Singularity container support for HPC";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      nativeDeps = pkgs: [
        # Add native dependencies here that your Python packages need
        # pkgs.stdenv.cc.cc.lib
      ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      pyprojectOverlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python312;
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            pyprojectOverlay
          ]
        )
      );

    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          virtualenv = pythonSets.${system}.mkVirtualEnv "dev-env" workspace.deps.default;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSets.${system}.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
            '';
          };
        }
      );

      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          virtualenv = pythonSets.${system}.mkVirtualEnv "runtime-env" workspace.deps.default;
          pname = workspace.meta.name or "app";
          version = workspace.meta.version or "0.1.0";
        in
        {
          default = virtualenv;

          container = pkgs.dockerTools.buildImage {
            name = pname;
            tag = version;
            created = "now";

            copyToRoot = pkgs.buildEnv {
              name = "image-root";
              paths = [
                virtualenv
                pkgs.coreutils
                pkgs.bashInteractive
              ] ++ (nativeDeps pkgs);
              pathsToLink = [
                "/bin"
                "/lib"
                "/lib64"
              ];
            };

            config = {
               Env = [
                 "PATH=/bin"
                 "PYTHONPATH=/work/src"
                 "LANG=C.UTF-8"
                 "LC_ALL=C.UTF-8"
               ];
               WorkingDir = "/work";
             };
          };
        }
      );
    };
}

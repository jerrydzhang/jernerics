{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.jernerics.mlflow;

  inherit (lib)
    mkEnableOption
    mkOption
    mkIf
    types
    ;

  authConfig = pkgs.writeText "basic_auth.ini" ''
    [mlflow]
    default_permission = READ
    database_uri = sqlite:///${cfg.stateDir}/basic_auth.db
    admin_username = ${cfg.adminUsername}
    admin_password = _
    authorization_function = mlflow.server.auth:authenticate_request_basic_auth
  '';

  # We can't put the real password in the nix store, so we generate the
  # final INI at service startup by splicing in the password from the file.
  startScript = pkgs.writeShellScript "mlflow-server-start" ''
    PASSWORD=$(cat "${cfg.adminPasswordFile}")

    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    sed "s/^admin_password = .*/admin_password = $PASSWORD/" ${authConfig} > "$TMPDIR/basic_auth.ini"

    export MLFLOW_AUTH_CONFIG_PATH="$TMPDIR/basic_auth.ini"
    export MLFLOW_FLASK_SERVER_SECRET_KEY="${builtins.hashString "sha256" cfg.stateDir}"

    exec ${cfg.package}/bin/mlflow server \
      --backend-store-uri "${cfg.backendStoreUri}" \
      --host "${cfg.host}" \
      --port ${toString cfg.port} \
      ${lib.optionalString cfg.serveArtifacts "--serve-artifacts"} \
      --app-name basic-auth \
      --allowed-hosts "*"
  '';
in
{
  options.services.jernerics.mlflow = {
    enable = mkEnableOption "MLflow tracking server";

    package = mkOption {
      type = types.package;
      default =
        let
          # nixpkgs builds mlflow from source, which omits the JS frontend.
          # Install from the PyPI wheel instead, which includes pre-built UI assets.
          mlflow-wheel = pkgs.python3.pkgs.buildPythonPackage rec {
            pname = "mlflow";
            version = pkgs.python3.pkgs.mlflow.version;
            format = "wheel";
            src = pkgs.fetchurl {
              url = "https://files.pythonhosted.org/packages/d5/d5/a20b87c6cd99395fee04d6034686512305530c71ceaabe3a151eeaa25ed7/mlflow-3.8.1-py3-none-any.whl";
              hash = "sha256-QvJrUkOP22FViOFQQHxlFtD2TUF0Nt/HVZnFJaRk8hA=";
            };
            # Inherit dependencies from the nixpkgs package
            propagatedBuildInputs = pkgs.python3.pkgs.mlflow.propagatedBuildInputs;
            nativeBuildInputs = pkgs.python3.pkgs.mlflow.nativeBuildInputs;
            # Wheel is pre-built — skip build phase
            dontBuild = true;
          };
        in
        pkgs.python3.withPackages (ps: [ mlflow-wheel ps.flask-wtf ]);
      defaultText = lib.literalExpression "pkgs.python3.withPackages (ps: [ ps.mlflow ps.flask-wtf ])";
      description = "Python environment containing mlflow (with UI) and auth dependencies.";
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Host address to bind.";
    };

    port = mkOption {
      type = types.port;
      default = 5000;
      description = "Port to listen on.";
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/mlflow";
      description = "Directory for MLflow state (auth DB, default artifact store).";
    };

    backendStoreUri = mkOption {
      type = types.str;
      default = "sqlite:///${cfg.stateDir}/mlflow.db";
      defaultText = lib.literalExpression ''"sqlite:///${cfg.stateDir}/mlflow.db"'';
      description = "Backend store URI. Defaults to local SQLite.";
    };

    serveArtifacts = mkOption {
      type = types.bool;
      default = true;
      description = "Serve artifacts via the tracking server.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the MLflow port in the firewall.";
    };

    adminUsername = mkOption {
      type = types.str;
      default = "admin";
      description = "Admin username for basic auth.";
    };

    adminPasswordFile = mkOption {
      type = types.path;
      description = "Path to a file containing the admin password. sops-nix compatible.";
      example = "/run/secrets/mlflow-admin-password";
    };


  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.adminPasswordFile != null;
        message = "services.jernerics.mlflow.adminPasswordFile must be set.";
      }
    ];

    users.users.mlflow = {
      isSystemUser = true;
      group = "mlflow";
      home = cfg.stateDir;
    };
    users.groups.mlflow = { };

    systemd.services.mlflow = {
      description = "MLflow tracking server";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        User = "mlflow";
        Group = "mlflow";
        ExecStart = startScript;
        StateDirectory = "mlflow";
        WorkingDirectory = cfg.stateDir;
        Restart = "on-failure";
        RestartSec = 5;

        # Hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadPaths = [ cfg.adminPasswordFile ];
      };
    };

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];
  };
}

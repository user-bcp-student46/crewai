"""S1 CrewAI — Generador BCP con HITL y checkpoint por paso.

Flujo:
  1. Conecta al MCP Projects → lista proyectos → elige proyecto → lista backlog → elige feature
  2. Por cada agente (Arquitecto, Backend, Frontend, Pruebas, Seguridad, Chapter), corre un mini-Crew de un solo paso.
  3. Tras cada paso muestra un preview y pide aprobación humana [s / n / r]:
       s = aprobar y continuar
       n = rechazar y abortar
       r = reintentar el mismo paso
  4. Al aprobar: guarda el artefacto inmediatamente (spec .md / código .java .ts .sql)
     y registra el checkpoint en output/.checkpoint.json.
  5. Al volver a ejecutar: carga el checkpoint y omite los pasos ya aprobados,
     inyectando sus outputs como contexto para los pasos restantes.
  6. Si quedan cambios en output/ sin integrar, ofrece el paso DevOps (PR hacia ai-labs): al arrancar,
     solo ese paso; o al terminar el flujo completo.

Estructura generada en output/:
  .checkpoint.json        ← estado de cada paso
  specs/
    user_story.md
    backend_spec.md
    frontend_spec.md
    chapter_review.md
  backend-quarkus/src/main/java/pe/bcp/<feature>/...
  frontend-angular/src/app/<feature>/...
  db/schema.sql  seed.sql
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import os
import logging

# ---------------------------------------------------------------------------
# Telemetría configurable por entorno
# ---------------------------------------------------------------------------
os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ["OTEL_LOG_LEVEL"] = "error"                # No ensucia la terminal con logs informativos
os.environ["OTEL_BSP_EXPORT_TIMEOUT"] = "2000"        # Reduce timeout de exportación a 2s (evita cuelgues al salir)

# Silenciar loggers que puedan imprimir advertencias de red/telemetría en stderr
for _log in ("opentelemetry", "crewai.telemetry", "urllib3", "requests"):
    logging.getLogger(_log).setLevel(logging.ERROR)


import re
import json
import time
import threading
import textwrap
from pathlib import Path
from crewai import Crew, Process

from crew.workflow import build_tasks, include_data, workflow_steps
from agents.architect import agent as architect_agent
from agents import devops as devops_agent
import integration

# ── MCP clients ────────────────────────────────────────────────────
from mcp_projects import (
    list_tools as proj_tools,
    list_projects,
    list_project_items,
    get_feature_from_item,
)
from mcp_github import create_pull_request
from hex_archetype import HEX_PROMPT_PREFIX

# ── Flag de verbosidad MCP ─────────────────────────────────────────
# True  → muestra el detalle de cada herramienta al arrancar
# False → solo confirma "Herramientas cargadas correctamente."
VERBOSE_MCP_TOOLS = False

# ── Scaffolding determinista para Backend y Frontend ────────────────
QUARKUS_POM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <groupId>pe.bcp</groupId>
    <artifactId>backend-quarkus</artifactId>
    <version>1.0.0-SNAPSHOT</version>

    <properties>
        <compiler-plugin.version>3.12.1</compiler-plugin.version>
        <maven.compiler.release>17</maven.compiler.release>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
        <project.reporting.outputEncoding>UTF-8</project.reporting.outputEncoding>
        <quarkus.platform.artifact-id>quarkus-bom</quarkus.platform.artifact-id>
        <quarkus.platform.group-id>io.quarkus.platform</quarkus.platform.group-id>
        <quarkus.platform.version>3.8.3</quarkus.platform.version>
    </properties>

    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>${quarkus.platform.group-id}</groupId>
                <artifactId>${quarkus.platform.artifact-id}</artifactId>
                <version>${quarkus.platform.version}</version>
                <type>pom</type>
                <scope>import</scope>
            </dependency>
        </dependencies>
    </dependencyManagement>

    <dependencies>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-spring-web</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-spring-di</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-spring-data-jpa</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-resteasy-reactive-jackson</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-hibernate-validator</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-jdbc-h2</artifactId>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-jdbc-postgresql</artifactId>
        </dependency>
        <dependency>
            <groupId>org.mindrot</groupId>
            <artifactId>jbcrypt</artifactId>
            <version>0.4</version>
        </dependency>
        <dependency>
            <groupId>io.quarkus</groupId>
            <artifactId>quarkus-junit5</artifactId>
            <scope>test</scope>
        </dependency>
    </dependencies>

    <build>
        <plugins>
            <plugin>
                <groupId>${quarkus.platform.group-id}</groupId>
                <artifactId>quarkus-maven-plugin</artifactId>
                <version>${quarkus.platform.version}</version>
                <extensions>true</extensions>
                <executions>
                    <execution>
                        <goals>
                            <goal>build</goal>
                            <goal>generate-code</goal>
                            <goal>generate-code-tests</goal>
                        </goals>
                    </execution>
                </executions>
            </plugin>
        </plugins>
    </build>
</project>
"""

QUARKUS_APP_PROPS_TEMPLATE = """# BCP Quarkus Backend Configuration
quarkus.application.name=bcp-login-service
quarkus.http.port=8080

# CORS habilitado para el frontend Angular local
quarkus.http.cors=true
quarkus.http.cors.origins=http://localhost:4200,http://127.0.0.1:4200
quarkus.http.cors.headers=accept, authorization, content-type, x-requested-with
quarkus.http.cors.methods=GET, POST, PUT, DELETE, OPTIONS

# Base de datos H2 en memoria para pruebas instantáneas sin configuración externa
quarkus.datasource.db-kind=h2
quarkus.datasource.jdbc.url=jdbc:h2:mem:logindb;DB_CLOSE_DELAY=-1
quarkus.hibernate-orm.database.generation=drop-and-create
quarkus.hibernate-orm.log.sql=true
quarkus.log.level=INFO
"""

ANGULAR_PACKAGE_JSON = """{
  "name": "frontend-angular",
  "version": "1.0.0",
  "scripts": {
    "ng": "ng",
    "start": "ng serve",
    "build": "ng build",
    "watch": "ng build --watch --configuration development"
  },
  "private": true,
  "dependencies": {
    "@angular/animations": "^17.3.0",
    "@angular/common": "^17.3.0",
    "@angular/compiler": "^17.3.0",
    "@angular/core": "^17.3.0",
    "@angular/forms": "^17.3.0",
    "@angular/platform-browser": "^17.3.0",
    "@angular/platform-browser-dynamic": "^17.3.0",
    "@angular/router": "^17.3.0",
    "rxjs": "~7.8.0",
    "tslib": "^2.3.0",
    "zone.js": "~0.14.3"
  },
  "devDependencies": {
    "@angular-devkit/build-angular": "^17.3.0",
    "@angular/cli": "^17.3.0",
    "@angular/compiler-cli": "^17.3.0",
    "typescript": "~5.4.2"
  }
}
"""

ANGULAR_JSON = """{
  "$schema": "./node_modules/@angular/cli/lib/config/schema.json",
  "version": 1,
  "newProjectRoot": "projects",
  "projects": {
    "frontend-angular": {
      "projectType": "application",
      "root": "",
      "sourceRoot": "src",
      "prefix": "app",
      "architect": {
        "build": {
          "builder": "@angular-devkit/build-angular:application",
          "options": {
            "outputPath": "dist/frontend-angular",
            "index": "src/index.html",
            "browser": "src/main.ts",
            "polyfills": ["zone.js"],
            "tsConfig": "tsconfig.app.json",
            "styles": ["src/styles.css"]
          },
          "configurations": {
            "production": {
              "outputHashing": "all"
            },
            "development": {
              "optimization": false,
              "sourceMap": true
            }
          },
          "defaultConfiguration": "production"
        },
        "serve": {
          "builder": "@angular-devkit/build-angular:dev-server",
          "configurations": {
            "production": {
              "buildTarget": "frontend-angular:build:production"
            },
            "development": {
              "buildTarget": "frontend-angular:build:development"
            }
          },
          "defaultConfiguration": "development"
        }
      }
    }
  }
}
"""

ANGULAR_TSCONFIG_JSON = """{
  "compileOnSave": false,
  "compilerOptions": {
    "outDir": "./dist/out-tsc",
    "strict": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "experimentalDecorators": true,
    "moduleResolution": "node",
    "target": "ES2022",
    "module": "ES2022",
    "lib": ["ES2022", "dom"]
  }
}
"""

ANGULAR_TSCONFIG_APP_JSON = """{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "outDir": "./out-tsc/app",
    "types": []
  },
  "files": ["src/main.ts"],
  "include": ["src/**/*.d.ts"]
}
"""

ANGULAR_INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>BCP - Portal de Autenticación</title>
  <base href="/">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body>
  <app-root></app-root>
</body>
</html>
"""

ANGULAR_STYLES_CSS = """* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', sans-serif;
  background: linear-gradient(135deg, #0b1f3a 0%, #002a66 50%, #0a438c 100%);
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
}
"""

ANGULAR_MAIN_TS = """import { bootstrapApplication } from '@angular/platform-browser';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { AppComponent } from './app/app.component';
import { routes } from './app/app.routes';

bootstrapApplication(AppComponent, {
  providers: [
    provideRouter(routes, withComponentInputBinding()),
    provideHttpClient(withInterceptorsFromDi())
  ]
}).catch(err => console.error(err));
"""

# El codigo de aplicacion (app.component.ts, app.routes.ts, el .css del
# componente y UserRepository.java) lo genera el modelo: depende del feature.
# Aca solo queda el andamiaje de build, que ningun agente tiene que escribir.


def ensure_scaffolding(output_dir: Path) -> None:
    """
    Escribe sólo el andamiaje de build: pom.xml y application.properties de
    Quarkus, y package.json / angular.json / tsconfig / index.html / main.ts de
    Angular. Nada de esto describe el feature, así que no se le pide al modelo.

    El código de aplicación —entidad, repositorio, servicio, componentes, rutas
    y estilos— lo genera el agente y lo escribe extract_and_save_code(). Si el
    agente no lo emite, el proyecto queda incompleto a propósito: es la señal
    de que el paso falló, no algo que el andamiaje deba tapar.
    """
    # 1. Backend Quarkus
    pom_path = output_dir / "backend-quarkus" / "pom.xml"
    if not pom_path.exists():
        pom_path.parent.mkdir(parents=True, exist_ok=True)
        pom_path.write_text(QUARKUS_POM_TEMPLATE, encoding="utf-8")
        print("  🏗️ Scaffolding Backend: generado pom.xml (Quarkus 3.x)")

    app_props_path = output_dir / "backend-quarkus" / "src" / "main" / "resources" / "application.properties"
    if not app_props_path.exists():
        app_props_path.parent.mkdir(parents=True, exist_ok=True)
        app_props_path.write_text(QUARKUS_APP_PROPS_TEMPLATE, encoding="utf-8")
        print("  🏗️ Scaffolding Backend: generado application.properties (CORS + H2)")

    # 2. Frontend Angular 17
    front_dir = output_dir / "frontend-angular"
    front_scaffold = {
        "package.json": ANGULAR_PACKAGE_JSON,
        "angular.json": ANGULAR_JSON,
        "tsconfig.json": ANGULAR_TSCONFIG_JSON,
        "tsconfig.app.json": ANGULAR_TSCONFIG_APP_JSON,
        "src/index.html": ANGULAR_INDEX_HTML,
        "src/styles.css": ANGULAR_STYLES_CSS,
        "src/main.ts": ANGULAR_MAIN_TS,
    }
    created_front = 0
    for rel_path, content in front_scaffold.items():
        file_path = front_dir / rel_path
        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            created_front += 1

    if created_front > 0:
        print(f"  🏗️ Scaffolding Frontend: generados {created_front} archivos base de Angular 17")


# ── Rutas base ─────────────────────────────────────────────────────
OUTPUT_DIR      = Path(__file__).parent / "output"
CHECKPOINT_FILE = OUTPUT_DIR / ".checkpoint.json"
OUTPUT_DIR.mkdir(exist_ok=True)   # lo necesita save_checkpoint()
# ensure_scaffolding() NO se llama aquí: escribiría en output/ con sólo importar
# el módulo, antes del MCP, del feature elegido y del primer agente.

# ── Mapa archivo → ruta destino ────────────────────────────────────
FILE_MAP = {
    "User.java":               "backend-quarkus/src/main/java/pe/bcp/login/model/User.java",
    "LoginRequest.java":       "backend-quarkus/src/main/java/pe/bcp/login/dto/LoginRequest.java",
    "AuthResponse.java":       "backend-quarkus/src/main/java/pe/bcp/login/dto/AuthResponse.java",
    "AuthService.java":        "backend-quarkus/src/main/java/pe/bcp/login/service/AuthService.java",
    "AuthResource.java":       "backend-quarkus/src/main/java/pe/bcp/login/resource/AuthResource.java",
    "pom.xml":                 "backend-quarkus/pom.xml",
    "application.properties":  "backend-quarkus/src/main/resources/application.properties",
    "models.ts":               "frontend-angular/src/app/auth/models.ts",
    "auth.service.ts":         "frontend-angular/src/app/auth/auth.service.ts",
    "auth.guard.ts":           "frontend-angular/src/app/auth/auth.guard.ts",
    "login.component.ts":      "frontend-angular/src/app/auth/login.component.ts",
    "login.component.html":    "frontend-angular/src/app/auth/login.component.html",
    "auth.interceptor.ts":     "frontend-angular/src/app/core/auth.interceptor.ts",
    "app.component.ts":        "frontend-angular/src/app/app.component.ts",
    "app.routes.ts":           "frontend-angular/src/app/app.routes.ts",
    "login.component.css":     "frontend-angular/src/app/auth/login.component.css",
    "UserRepository.java":     "backend-quarkus/src/main/java/pe/bcp/login/repository/UserRepository.java",
    "schema.sql":              "db/schema.sql",
    "seed.sql":                "db/seed.sql",
}

# ── Checkpoint ─────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    """Carga el checkpoint si existe, o retorna estado vacío."""
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"steps": {}}


def save_checkpoint(data: dict) -> None:
    """Persiste el checkpoint en disco de forma segura."""
    try:
        clean_data = {}
        for k, v in data.items():
            if k == "steps" and isinstance(v, dict):
                clean_steps = {}
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        clean_steps[sk] = {
                            "status": sv.get("status"),
                            "output": str(getattr(sv.get("output"), "raw", sv.get("output")) or "")
                        }
                    else:
                        clean_steps[sk] = str(sv)
                clean_data[k] = clean_steps
            else:
                clean_data[k] = v
        CHECKPOINT_FILE.write_text(json.dumps(clean_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠ Advertencia al guardar checkpoint: {e}")


# ── Output helpers ─────────────────────────────────────────────────

def extract_and_save_code(text) -> list[str]:
    """Extrae bloques de código del output del LLM y los guarda en sus carpetas correspondientes."""
    text_str = str(getattr(text, "raw", text) or "")
    saved = []
    seen_paths = set()

    # Patrón para bloques ``` con o sin lenguaje
    code_block_pattern = re.compile(
        r"(?:(?:archivo|file|fichero|clase|class|componente)?[:\s*#/-]*[`\"]?([a-zA-Z0-9_.\-/]+\.(?:java|ts|html|css|sql|xml|properties))[`\"]?[^\n]*\n)?"
        r"```(?:[a-zA-Z0-9_\-]+)?\n(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )

    for m in code_block_pattern.finditer(text_str):
        name = m.group(1)
        code = m.group(2).strip()
        if not code:
            continue

        target_name = name

        # 1. Buscar nombre en las primeras líneas del bloque de código (incluyendo rutas con slashes)
        if not target_name:
            lines = code.split("\n")[:5]
            for l in lines:
                m_line = re.search(
                    r"(?:(?:\/\/|\/\*|#|<!--|--)\s*|\b)([a-zA-Z0-9_.\-/]+\.(?:java|ts|html|css|sql|xml|properties))\b",
                    l,
                    re.IGNORECASE,
                )
                if m_line:
                    target_name = m_line.group(1)
                    break

        # 2. Heurística según el contenido del código si no se detectó nombre explícito
        if not target_name:
            m_java = re.search(r"public\s+(?:class|interface|record|enum)\s+(\w+)", code)
            if m_java:
                target_name = f"{m_java.group(1)}.java"
            elif "CREATE TABLE" in code.upper():
                target_name = "schema.sql"
            elif "INSERT INTO" in code.upper():
                target_name = "seed.sql"
            elif re.search(r"export\s+class\s+(\w+Component)", code) or "@Component" in code:
                target_name = "login.component.ts"
            elif re.search(r"export\s+class\s+(\w+Service)", code) or ("@Injectable" in code and "service" in code.lower()):
                target_name = "auth.service.ts"
            elif re.search(r"export\s+class\s+(\w+Guard)", code) or "CanActivate" in code:
                target_name = "auth.guard.ts"
            elif "HttpInterceptor" in code or "Interceptor" in code:
                target_name = "auth.interceptor.ts"
            elif "export interface" in code or "export type" in code:
                target_name = "models.ts"
            elif "<form" in code.lower() or ("<button" in code.lower() and "<div" in code.lower()):
                target_name = "login.component.html"

        if not target_name:
            continue

        # Extraer solo el nombre base del archivo
        target_name = Path(target_name.strip()).name

        # Mapeo a ruta destino
        dest_rel = FILE_MAP.get(target_name)
        if not dest_rel:
            lname = target_name.lower()
            if target_name.endswith(".java"):
                if "test" in lname or "@test" in code.lower():
                    dest_rel = f"backend-quarkus/src/test/java/pe/bcp/login/{target_name}"
                elif "service" in lname:
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/service/{target_name}"
                elif "resource" in lname or "controller" in lname or "@path" in code.lower():
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/resource/{target_name}"
                elif "repository" in lname or "dao" in lname:
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/repository/{target_name}"
                elif "request" in lname or "response" in lname or "dto" in lname:
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/dto/{target_name}"
                elif "model" in lname or "entity" in lname or "@entity" in code.lower():
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/model/{target_name}"
                else:
                    # Por defecto clasificar como modelo de dominio
                    dest_rel = f"backend-quarkus/src/main/java/pe/bcp/login/model/{target_name}"
            elif target_name.endswith(".ts"):
                if "interceptor" in lname:
                    dest_rel = f"frontend-angular/src/app/core/{target_name}"
                elif "guard" in lname:
                    dest_rel = f"frontend-angular/src/app/auth/{target_name}"
                elif "service" in lname:
                    dest_rel = f"frontend-angular/src/app/auth/{target_name}"
                elif "component" in lname:
                    dest_rel = f"frontend-angular/src/app/auth/{target_name}"
                else:
                    dest_rel = f"frontend-angular/src/app/auth/{target_name}"
            elif target_name.endswith(".html"):
                dest_rel = f"frontend-angular/src/app/auth/{target_name}"
            elif target_name.endswith(".css"):
                dest_rel = f"frontend-angular/src/app/auth/{target_name}"
            elif target_name.endswith(".sql"):
                dest_rel = f"db/{target_name}"
            elif target_name.endswith(".properties"):
                dest_rel = f"backend-quarkus/src/main/resources/{target_name}"
            elif target_name == "pom.xml":
                dest_rel = f"backend-quarkus/{target_name}"

        if dest_rel and dest_rel not in seen_paths:
            seen_paths.add(dest_rel)

            # Normalización automática para garantizar compilabilidad directa
            if dest_rel.endswith("models.ts"):
                code = re.sub(r'^(interface\s+)', r'export \1', code, flags=re.MULTILINE)
            elif dest_rel.endswith(".java") and not re.search(r'^\s*package\s+', code, flags=re.MULTILINE):
                if "/model/" in dest_rel:
                    pkg = "pe.bcp.login.model"
                elif "/service/" in dest_rel:
                    pkg = "pe.bcp.login.service"
                elif "/resource/" in dest_rel:
                    pkg = "pe.bcp.login.resource"
                elif "/repository/" in dest_rel:
                    pkg = "pe.bcp.login.repository"
                elif "/dto/" in dest_rel:
                    pkg = "pe.bcp.login.dto"
                else:
                    pkg = "pe.bcp.login"
                code = f"package {pkg};\n\n" + code

            dest = OUTPUT_DIR / dest_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            saved.append(dest_rel)
            print(f"  💾 Guardado: {dest_rel}")

    if not saved:
        print("  ⚠ Advertencia: no se detectaron archivos de código fuente para extraer en este paso.")
    else:
        print(f"  📦 Total archivos de código generados en este paso: {len(saved)}")

    # Asegurar que el andamiaje base exista siempre
    ensure_scaffolding(OUTPUT_DIR)

    return saved


def save_spec(filename: str, content) -> None:
    specs_dir = OUTPUT_DIR / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    text = str(getattr(content, "raw", content) or "")
    (specs_dir / filename).write_text(text, encoding="utf-8")
    print(f"  ✅ specs/{filename}")


# ── Mini-Crew: corre un único agente/tarea ─────────────────────────

class StepLoadingSpinner:
    """Indicador de carga interactivo con spinner y tiempo transcurrido en tiempo real."""
    def __init__(self, message: str = "Agente procesando con el LLM..."):
        self.message = message
        self.stop_event = threading.Event()
        self.thread = None

    def _spin(self):
        chars = ["|", "/", "-", "\\"]
        idx = 0
        t0 = time.time()
        # Pequeña pausa inicial para que CrewAI imprima sus recuadros iniciales
        time.sleep(1.0)
        while not self.stop_event.is_set():
            elapsed = int(time.time() - t0)
            mins, secs = divmod(elapsed, 60)
            time_str = f"{mins:02d}:{secs:02d}"
            char = chars[idx % len(chars)]
            msg = f"\r  ⏳ [{char}] {self.message} ({time_str} transcurrido)   "
            try:
                sys.stdout.write(msg)
                sys.stdout.flush()
            except Exception:
                pass
            idx += 1
            time.sleep(0.2)
        try:
            sys.stdout.write("\r" + " " * 75 + "\r")
            sys.stdout.flush()
        except Exception:
            pass

    def __enter__(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)


def run_step(agent, task) -> str:
    """Ejecuta un mini-Crew de un solo agente y retorna el output en texto."""
    mini_crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
        tracing=False,
    )
    with StepLoadingSpinner(f"Generando con {agent.llm.model}..."):
        result = mini_crew.kickoff()

    # Pausa para permitir que los callbacks y paneles asíncronos de CrewAI terminen de pintar en consola
    time.sleep(1.2)

    if hasattr(result, "raw") and result.raw:
        return str(result.raw)
    if hasattr(task, "output") and task.output:
        return str(getattr(task.output, "raw", task.output))
    return str(result)


# ── HITL: aprobación humana ────────────────────────────────────────

def _flush_input_buffer():
    """Limpia caracteres tecleados accidentalmente mientras el LLM generaba."""
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except Exception:
        pass


def print_formatted_preview(step_name: str, output: str, max_lines: int = 45) -> None:
    """Muestra el preview con formato enriquecido (tablas redondeadas alineadas, colores, preservando saltos de línea)."""
    output_str = str(getattr(output, "raw", output) or "").strip()
    if not output_str:
        print("  (Sin contenido generado)")
        return

    try:
        from rich.console import Console
        from rich.table import Table
        from rich.box import ROUNDED

        console = Console()
        console.print(f"\n[bold cyan]{'─'*65}[/bold cyan]")
        console.print(f"[bold cyan]▶ OUTPUT GENERADO — {step_name}[/bold cyan]")
        console.print(f"[bold cyan]{'─'*65}[/bold cyan]\n")

        all_lines = output_str.splitlines()
        lines = all_lines[:max_lines]
        hidden_count = max(0, len(all_lines) - max_lines)

        table_lines = []
        in_table = False
        non_table_buffer = []

        def flush_buf():
            if non_table_buffer:
                console.print("\n".join(non_table_buffer))
                non_table_buffer.clear()

        def render_table(tbl_lines):
            if len(tbl_lines) < 2:
                for tl in tbl_lines:
                    console.print(tl)
                return
            headers = [c.strip() for c in tbl_lines[0].split("|")[1:-1]]
            if not headers:
                for tl in tbl_lines:
                    console.print(tl)
                return
            table = Table(show_header=True, header_style="bold magenta", box=ROUNDED, expand=True)
            for i, h in enumerate(headers):
                if i == 0:
                    table.add_column(h, style="cyan", ratio=3)
                elif i == 1 and any(k in h.lower() for k in ("estado", "status", "resultado")):
                    table.add_column(h, justify="center", ratio=1)
                else:
                    table.add_column(h, style="white", ratio=4)

            # Las filas de datos comienzan tras el separador | --- |
            for r in tbl_lines[2:]:
                cols = [c.strip() for c in r.split("|")[1:-1]]
                if len(cols) == len(headers):
                    table.add_row(*cols)
                elif cols:
                    while len(cols) < len(headers):
                        cols.append("")
                    table.add_row(*cols[:len(headers)])
            console.print(table)

        for line in lines:
            st = line.strip()
            if st.startswith("|") and st.endswith("|") and st.count("|") >= 2:
                if not in_table:
                    flush_buf()
                    in_table = True
                table_lines.append(st)
            else:
                if in_table:
                    render_table(table_lines)
                    table_lines.clear()
                    in_table = False
                non_table_buffer.append(line)

        if in_table:
            render_table(table_lines)
        flush_buf()

        if hidden_count > 0:
            console.print(f"\n[dim italic]... [{hidden_count} líneas adicionales guardadas en specs/ y código][/dim italic]")

        console.print(f"\n[bold cyan]{'─'*65}[/bold cyan]")

    except Exception:
        # Fallback sin formato especial si falla Rich
        sep = "─" * 65
        print(f"\n{sep}\n▶ OUTPUT GENERADO — {step_name}\n{sep}")
        print("\n".join(output_str.splitlines()[:max_lines]))
        print(sep)


def hitl_approve(step_name: str, output: str) -> tuple[str, str]:
    """
    Muestra un preview del output y pide aprobación de forma clara e interactiva.

    Retorna:
        ("s", "")         → aprobado
        ("n", "")         → rechazado (abortar)
        ("r", feedback)   → reintentar el paso (con feedback opcional del usuario)
    """
    import time
    time.sleep(0.3)

    print_formatted_preview(step_name, output)

    box_line = "=" * 65
    banner = f"""
{box_line}
 🔔 ACCIÓN REQUERIDA — Aprobación humana para: {step_name}
{box_line}
 [s] = Aprobar y continuar al siguiente paso
 [r] = Reintentar este mismo paso (permite ingresar feedback opcional)
 [n] = Rechazar y detener ejecución
{box_line}"""
    print(banner)

    while True:
        _flush_input_buffer()
        try:
            raw = input("\n👉 Ingresa tu opción [s/r/n] y presiona Enter: ").strip().lower()
        except EOFError:
            return "s", ""
        except KeyboardInterrupt:
            print("\n❌ Interrupción por teclado.")
            return "n", ""

        tokens = raw.split()
        choice = tokens[-1] if tokens else ""

        if not choice:
            continue

        if choice in ("s", "si", "sí", "y", "yes", "1") or choice.endswith("s"):
            return "s", ""
        if choice in ("n", "no", "0") or choice.endswith("n"):
            return "n", ""
        if choice in ("r", "retry", "reintentar") or choice.endswith("r"):
            print("\n💬 [Feedback Opcional] Escribe instrucciones adicionales para el agente")
            feedback = input("   (o presiona Enter para reintentar sin cambios): ").strip()
            return "r", feedback

        print("\n  ✗ Opción inválida. Por favor ingresa 's' para sí, 'r' para reintentar o 'n' para rechazar.")



# ── Contexto acumulado ─────────────────────────────────────────────

def _ctx_snippet(text: str, max_chars: int = 8000) -> str:
    """Recorta el output a max_chars para inyectarlo como contexto."""
    text_clean = str(text or "").strip()
    if len(text_clean) <= max_chars:
        return text_clean
    return text_clean[:max_chars] + "\n...(truncado para contexto)"


def build_context_note(approved_outputs: dict) -> str:
    """Construye una nota de contexto con los outputs aprobados anteriores."""
    parts = []
    labels = {key: label for key, label, *_ in workflow_steps(True)}
    for key, label in labels.items():
        if key in approved_outputs:
            val = str(getattr(approved_outputs[key], "raw", approved_outputs[key]) or "")
            parts.append(f"### {label}\n{_ctx_snippet(val)}")
    if not parts:
        return ""
    return "\n\n---\n## Contexto de pasos anteriores\n\n" + "\n\n".join(parts)



# ═══════════════════════════════════════════════════════════════════
# DEVOPS — integración vía PR
# ═══════════════════════════════════════════════════════════════════

def _ask_yes_no(question: str) -> bool:
    """Solo una afirmación explícita cuenta como sí; cualquier otra cosa es no."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("s", "si", "sí", "y", "yes")


def _approved_outputs(checkpoint: dict) -> dict:
    return {k: v.get("output", "") for k, v in checkpoint.get("steps", {}).items()
            if isinstance(v, dict) and v.get("status") == "approved"}


def _flow_complete(checkpoint: dict, steps) -> bool:
    approved = _approved_outputs(checkpoint)
    return all(step[0] in approved for step in steps)


def _print_pending(pending: dict) -> None:
    print(f"  · {len(pending['new'])} archivo(s) nuevo(s), {len(pending['modified'])} modificado(s) desde la última integración.")
    for label, key in (("nuevo", "new"), ("modificado", "modified")):
        for path in pending[key][:10]:
            print(f"    - {label}: {path}")
        if len(pending[key]) > 10:
            print(f"    ... y {len(pending[key]) - 10} más ({label})")


def _feature_from_checkpoint(checkpoint: dict) -> dict:
    feature = checkpoint.get("feature")
    if isinstance(feature, dict) and feature.get("title"):
        return feature
    story = OUTPUT_DIR / "specs" / "user_story.md"
    description = story.read_text(encoding="utf-8")[:4000] if story.exists() else ""
    return {"title": checkpoint.get("feature_title", ""), "description": description, "acceptance_criteria": []}


def run_devops_step(feature: dict, checkpoint: dict) -> bool:
    """Paso DevOps: clona ai-labs, el agente redacta el PR, tras aprobación se sube la rama y se abre el PR."""
    owner = os.getenv("DEVOPS_TARGET_OWNER") or os.getenv("GITHUB_OWNER", "")
    repo  = os.getenv("DEVOPS_TARGET_REPO", "ai-labs")
    base  = os.getenv("DEVOPS_BASE_BRANCH", "main")

    print(f"\n{'─'*60}\n▶ PASO: {devops_agent.LABEL} → {owner}/{repo} (base: {base})\n{'─'*60}")
    files = integration.output_files(OUTPUT_DIR)
    try:
        staging = integration.stage_changes(OUTPUT_DIR, files, integration.repo_url(owner, repo), base)
    except (ValueError, RuntimeError) as exc:
        print(f"\n❌ No se pudo preparar la integración: {exc}")
        return False

    steps_state = checkpoint.setdefault("steps", {})
    approved_outputs = _approved_outputs(checkpoint)
    try:
        user_feedback = ""
        while True:
            task = devops_agent.build_task(feature)
            task.description += build_context_note(approved_outputs) + integration.describe_changes(staging)
            if user_feedback:
                task.description += (
                    f"\n\n---\n## ⚠️ INSTRUCCIONES ESPECÍFICAS DE CORRECCIÓN (FEEDBACK HUMANO):\n"
                    f"{user_feedback}\n"
                )

            output = run_step(devops_agent.agent, task)
            decision, user_feedback = hitl_approve(devops_agent.LABEL, output)

            if decision == "r":
                print("\n🔄 Reintentando...")
                continue
            if decision == "n":
                print(f"\n❌ {devops_agent.LABEL} rechazado. No se subió nada.")
                steps_state["devops"] = {"status": "rejected", "output": output}
                save_checkpoint(checkpoint)
                return False

            draft  = integration.parse_draft(output, feature.get("title", ""))
            branch = integration.unique_branch(draft.branch)
            try:
                integration.commit_and_push(staging, branch, draft.commit)
            except (ValueError, RuntimeError) as exc:
                print(f"\n❌ No se pudo subir la rama: {exc}")
                return False
            try:
                pr = create_pull_request(draft.title, draft.body, branch, base)
            except RuntimeError as exc:
                print(f"\n❌ La rama '{branch}' se subió, pero no se pudo abrir el PR: {exc}")
                return False

            pr_url = pr.get("url") or f"(PR #{pr.get('number')})"
            print(f"\n✅ PR abierto: {pr_url}")
            save_spec(devops_agent.SPEC_FILE, f"{output}\n\n---\nPR: {pr_url}\nRama: {branch}\n")
            steps_state["devops"] = {"status": "approved", "output": output}
            save_checkpoint(checkpoint)
            integration.save_manifest(OUTPUT_DIR, files, pr_url, branch)
            return True
    finally:
        integration.cleanup(staging)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    with_data = include_data()
    steps = workflow_steps(with_data)

    # ── Cambios por integrar de una ejecución anterior → ofrecer solo DevOps ──
    prior = load_checkpoint()
    mode_now = "with-data" if with_data else "standard"
    if prior.get("workflow_mode", "standard") == mode_now and _flow_complete(prior, steps):
        pending = integration.pending_changes(OUTPUT_DIR)
        if pending["new"] or pending["modified"]:
            print("\n[DevOps] Hay cambios generados por el flujo que aún no se integran en el repo destino:")
            _print_pending(pending)
            if _ask_yes_no("\n¿Iniciar el flujo solo por el paso DevOps (integración vía PR)? [s/n]: "):
                run_devops_step(_feature_from_checkpoint(prior), prior)
                return
    # ── MCP Proyectos: descubrir tools ────────────────────────────────
    print("\n[MCP Proyectos] Autenticando...")
    _proj_tools = proj_tools()
    if VERBOSE_MCP_TOOLS:
        print("[MCP Proyectos] Herramientas disponibles:")
        for t in _proj_tools:
            print(f"  · {t['name']}: {t['description']}")
    else:
        print("[MCP Proyectos] Herramientas cargadas correctamente.")

    # ── Selección de proyecto ─────────────────────────────────────────
    projects = list_projects()
    if not projects:
        print("[MCP Proyectos] ✗ No se encontraron proyectos.")
        raise SystemExit(1)

    if len(projects) == 1:
        selected_project = projects[0]
        print(f"\n[MCP Proyectos] Proyecto asignado: {selected_project['title']}")
    else:
        print("\n[MCP Proyectos] Proyectos disponibles:")
        for i, proj in enumerate(projects, 1):
            icon = "⚫" if proj.get("closed") else "🟢"
            print(f"  {i}. {icon} {proj.get('title', '(sin título)')}")
        while True:
            sel = input(f"\nSelecciona el número de proyecto [1-{len(projects)}]: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(projects):
                selected_project = projects[int(sel) - 1]
                break
            print(f"  ✗ Entrada inválida.")

    # ── Selección de feature del backlog ─────────────────────────────
    backlog = list_project_items(selected_project["id"])
    if not backlog:
        print("[MCP Proyectos] ✗ El proyecto no tiene items en el backlog.")
        raise SystemExit(1)

    print(f"\n[MCP Proyectos] Backlog de '{selected_project['title']}':")
    for i, item in enumerate(backlog, 1):
        icon = "⚫" if item.get("state", "").lower() == "closed" else "🟢"
        num  = f"#{item['number']} — " if item.get("number") else ""
        print(f"  {i}. {icon} {num}{item.get('title', '(sin título)')}")

    while True:
        sel = input(f"\nSelecciona el número de feature [1-{len(backlog)}]: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(backlog):
            selected_item = backlog[int(sel) - 1]
            break
        print(f"  ✗ Entrada inválida.")

    feature = get_feature_from_item(selected_item)
    print(f"\n[MCP Proyectos] Feature cargado: {feature['title']}")
    print(f"[MCP Proyectos] Criterios de aceptación: {feature['acceptance_criteria']}")

    # ── Cargar checkpoint ─────────────────────────────────────────────
    checkpoint  = load_checkpoint()
    steps_state = checkpoint.get("steps", {})

    # Verificar si el checkpoint corresponde a este feature (por título)
    ckpt_feature = checkpoint.get("feature_title")
    if ckpt_feature and ckpt_feature != feature["title"]:
        print(f"\n⚠  El checkpoint existente es para '{ckpt_feature}'.")
        ans = input("¿Ignorar checkpoint y empezar desde cero? [s/n]: ").strip().lower()
        if ans == "s":
            steps_state = {}
            checkpoint  = {"steps": {}}
        else:
            print("Abortando. Cambia de feature o elimina output/.checkpoint.json para reiniciar.")
            raise SystemExit(0)

    mode = "with-data" if with_data else "standard"
    previous_mode = checkpoint.get("workflow_mode", "standard")
    if steps_state and previous_mode != mode:
        print("El checkpoint pertenece a otro flujo. Conserva output/ moviéndolo a otra carpeta antes de cambiar CREWAI_INCLUDE_DATA.")
        raise SystemExit(1)
    checkpoint["workflow_mode"] = mode
    checkpoint["feature_title"] = feature["title"]
    checkpoint["feature"] = feature

    # ── Recuperar outputs aprobados del checkpoint ────────────────────
    approved_outputs: dict = {}
    for step_key, step_data in steps_state.items():
        if step_data.get("status") == "approved":
            approved_outputs[step_key] = step_data.get("output", "")

    # ── Enriquecer backstory del Chapter con brief del feature ────────
    architect_agent.backstory += HEX_PROMPT_PREFIX + (
        f"\n\n## Feature brief\n"
        f"**{feature['title']}**\n\n{feature['description']}\n\n"
        f"**Criterios de aceptación:** {', '.join(feature['acceptance_criteria'])}"
    )

    # ── Definición de pasos ───────────────────────────────────────────
    # (key, label, agent, spec_filename, extraer_código)


    # Andamiaje de build: en este punto, con proyecto y feature ya elegidos.
    ensure_scaffolding(OUTPUT_DIR)

    print("\n" + "=" * 60)
    print(f"S1 CrewAI — HITL — Feature: {feature['title']}")
    print("Pasos: " + " → ".join(step[1] for step in steps))
    print("=" * 60)

    # ── Loop HITL ─────────────────────────────────────────────────────
    for step_key, step_label, agent, spec_file, do_code in steps:

        # ── Ya aprobado en ejecución anterior → omitir ───────────────
        if steps_state.get(step_key, {}).get("status") == "approved":
            print(f"\n✅ [{step_label}] — ya aprobado (checkpoint). Omitiendo.")
            if do_code:
                saved_out = steps_state[step_key].get("output", "")
                if saved_out:
                    extract_and_save_code(saved_out)
            continue

        print(f"\n{'─'*60}")
        print(f"▶ PASO: {step_label}")
        print(f"{'─'*60}")

        # ── Bucle de reintentos ───────────────────────────────────────
        user_feedback = ""
        while True:
            # Rebuild tasks fresh en cada intento
            task_map = build_tasks(feature, with_data)
            task = task_map[step_key]

            # Inyectar contexto de pasos anteriores en la descripción
            ctx_note = build_context_note(approved_outputs)
            if ctx_note:
                task.description = task.description + ctx_note

            if with_data and step_key == "backend":
                task.description += "\nUsa el diseño de persistencia aprobado de Datos y persistencia. No regeneres sus archivos SQL; implementa el acceso a datos según ese contrato."
            if with_data and step_key == "chapter":
                task.description += "\nRevisa también el modelo, migración y compatibilidad del contrato aprobado de Datos y persistencia con backend."

            # Si el usuario ingresó feedback opcional en un reintento, inyectarlo como directiva prioritaria
            if user_feedback:
                task.description += (
                    f"\n\n---\n## ⚠️ INSTRUCCIONES ESPECÍFICAS DE CORRECCIÓN (FEEDBACK HUMANO):\n"
                    f"{user_feedback}\n"
                    f"Por favor atiende y corrige con máxima prioridad las observaciones del usuario indicadas arriba.\n"
                )

            # Ejecutar mini-Crew
            output = run_step(agent, task)

            # HITL
            decision, user_feedback = hitl_approve(step_label, output)

            if decision == "s":
                # APROBADO
                print(f"\n✅ {step_label} aprobado. Guardando artefactos...")
                save_spec(spec_file, output)
                if do_code:
                    extract_and_save_code(output)
                approved_outputs[step_key] = output
                steps_state[step_key] = {"status": "approved", "output": output}
                checkpoint["steps"] = steps_state
                save_checkpoint(checkpoint)
                break

            elif decision == "n":
                # RECHAZADO
                print(f"\n❌ {step_label} rechazado. Abortando ejecución.")
                steps_state[step_key] = {"status": "rejected", "output": output}
                checkpoint["steps"] = steps_state
                save_checkpoint(checkpoint)
                raise SystemExit(0)

            else:
                # REINTENTAR
                if user_feedback:
                    print(f"\n🔄 Reintentando {step_label} incorporando tu feedback: \"{user_feedback}\"...")
                else:
                    print(f"\n🔄 Reintentando {step_label}...")
                # el while True vuelve a ejecutar el paso

    # ── Todos los pasos aprobados ─────────────────────────────────────
    chapter_text = str(getattr(approved_outputs.get("chapter", ""), "raw", approved_outputs.get("chapter", "")) or "")

    print("\n" + "=" * 60)
    print("✅ TODOS LOS PASOS APROBADOS")
    print("=" * 60)
    print("  output/specs/            → " + " · ".join(step[3] for step in steps))
    print("  output/backend-quarkus/  → archivos .java extraídos")
    print("  output/frontend-angular/ → archivos .ts/.html extraídos")
    print("  output/db/               → .sql extraídos")
    print("=" * 60)
    print("\nVEREDICTO del Chapter (extracto):")
    print(chapter_text[-500:] if len(chapter_text) > 500 else chapter_text)

    pending = integration.pending_changes(OUTPUT_DIR)
    if pending["new"] or pending["modified"]:
        print("\n[DevOps] Hay cambios generados que aún no se integran en el repo destino:")
        _print_pending(pending)
        if _ask_yes_no("\n¿Integrarlos ahora vía PR? [s/n]: "):
            run_devops_step(feature, checkpoint)


if __name__ == "__main__":
    main()

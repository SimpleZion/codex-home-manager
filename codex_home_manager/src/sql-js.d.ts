declare module "sql.js" {
  export type QueryExecResult = {
    columns: string[];
    values: unknown[][];
  };

  export type SqlJsDatabase = {
    exec(sql: string, params?: unknown[]): QueryExecResult[];
    close(): void;
  };

  export type SqlJsStatic = {
    Database: new (data?: Uint8Array) => SqlJsDatabase;
  };

  export default function initSqlJs(config?: { locateFile?: (file: string) => string }): Promise<SqlJsStatic>;
}


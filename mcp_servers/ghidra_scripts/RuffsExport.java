// RUFFS: exporta a JSON lo que pide el servidor MCP de Ghidra (modo headless).
// Uso (lo invoca mcp_servers/ghidra_server.py):
//   -postScript RuffsExport.java <salida.json> <peticion.properties>
// La petición va en un archivo (no en la línea de órdenes) para que filtros y
// nombres con espacios o símbolos no se rompan al pasar por analyzeHeadless.bat.
// Claves: mode (summary | functions | decompile | disassemble | imports |
//         exports | strings | xrefs), target, filter, offset, limit.
//@category RUFFS
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolTable;

import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.io.FileOutputStream;

public class RuffsExport extends GhidraScript {

    private static String q(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder(s.length() + 2);
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    private java.util.Properties req = new java.util.Properties();

    private String str(String key) {
        String v = req.getProperty(key);
        return v == null ? "" : v;
    }

    private int num(String key, int def) {
        try { return Integer.parseInt(str(key).trim()); } catch (NumberFormatException e) { return def; }
    }

    private boolean match(String text, String filter) {
        return filter.isEmpty() || (text != null && text.toLowerCase().contains(filter));
    }

    private Function resolveFunction(String target) {
        if (target == null || target.isEmpty()) return null;
        try {
            Address addr = toAddr(target.startsWith("0x") || target.startsWith("0X") ? target.substring(2) : target);
            if (addr != null) {
                Function f = getFunctionContaining(addr);
                if (f != null) return f;
            }
        } catch (Exception ignored) { }
        Function loose = null;
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        while (it.hasNext()) {
            Function f = it.next();
            if (f.getName().equals(target)) return f;
            if (loose == null && f.getName().equalsIgnoreCase(target)) loose = f;
        }
        return loose;
    }

    private String fnJson(Function f) {
        return "{\"name\":" + q(f.getName()) + ",\"address\":" + q(f.getEntryPoint().toString())
            + ",\"size\":" + f.getBody().getNumAddresses()
            + ",\"thunk\":" + f.isThunk()
            + ",\"signature\":" + q(f.getSignature().getPrototypeString()) + "}";
    }

    private String summary() {
        StringBuilder b = new StringBuilder("{");
        b.append("\"name\":").append(q(currentProgram.getName()));
        b.append(",\"format\":").append(q(currentProgram.getExecutableFormat()));
        b.append(",\"language\":").append(q(currentProgram.getLanguageID().toString()));
        b.append(",\"compiler\":").append(q(currentProgram.getCompilerSpec().getCompilerSpecID().toString()));
        b.append(",\"image_base\":").append(q(currentProgram.getImageBase().toString()));
        b.append(",\"md5\":").append(q(currentProgram.getExecutableMD5()));
        b.append(",\"sha256\":").append(q(currentProgram.getExecutableSHA256()));
        b.append(",\"function_count\":").append(currentProgram.getFunctionManager().getFunctionCount());
        b.append(",\"memory_blocks\":[");
        boolean first = true;
        for (MemoryBlock m : currentProgram.getMemory().getBlocks()) {
            if (!first) b.append(',');
            first = false;
            String perms = (m.isRead() ? "r" : "-") + (m.isWrite() ? "w" : "-") + (m.isExecute() ? "x" : "-");
            b.append("{\"name\":").append(q(m.getName())).append(",\"start\":").append(q(m.getStart().toString()))
             .append(",\"end\":").append(q(m.getEnd().toString())).append(",\"size\":").append(m.getSize())
             .append(",\"perms\":").append(q(perms)).append('}');
        }
        b.append("],\"entry_points\":[");
        first = true;
        int n = 0;
        AddressIterator eps = currentProgram.getSymbolTable().getExternalEntryPointIterator();
        while (eps.hasNext() && n < 50) {
            Address a = eps.next();
            Symbol s = getSymbolAt(a);
            if (!first) b.append(',');
            first = false;
            b.append("{\"address\":").append(q(a.toString())).append(",\"name\":").append(q(s != null ? s.getName() : null)).append('}');
            n++;
        }
        return b.append("]}").toString();
    }

    private String functions(String filter, int offset, int limit) {
        StringBuilder b = new StringBuilder("{\"functions\":[");
        int total = 0, shown = 0;
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        while (it.hasNext()) {
            Function f = it.next();
            if (!match(f.getName(), filter)) continue;
            if (total >= offset && shown < limit) {
                if (shown > 0) b.append(',');
                b.append(fnJson(f));
                shown++;
            }
            total++;
        }
        return b.append("],\"total\":").append(total).append(",\"offset\":").append(offset)
                .append(",\"has_more\":").append(offset + shown < total).append('}').toString();
    }

    private String decompile(String target) {
        Function f = resolveFunction(target);
        if (f == null) return "{\"error\":" + q("Función no encontrada: " + target) + "}";
        DecompInterface ifc = new DecompInterface();
        try {
            if (!ifc.openProgram(currentProgram)) {
                // Normalmente: no se encuentra o no se puede ejecutar el decompilador nativo.
                return "{\"function\":" + fnJson(f) + ",\"error\":" + q("No se pudo iniciar el decompilador: " + ifc.getLastMessage()) + "}";
            }
            DecompileResults r = ifc.decompileFunction(f, 120, monitor);
            if (r == null || !r.decompileCompleted() || r.getDecompiledFunction() == null) {
                String why = r != null ? r.getErrorMessage() : "";
                if (why == null || why.isBlank()) why = ifc.getLastMessage();
                return "{\"function\":" + fnJson(f) + ",\"error\":" + q("Decompilación incompleta: " + why) + "}";
            }
            return "{\"function\":" + fnJson(f) + ",\"c\":" + q(r.getDecompiledFunction().getC()) + "}";
        } finally {
            ifc.dispose();
        }
    }

    private String disassemble(String target, int limit) {
        Function f = resolveFunction(target);
        if (f == null) return "{\"error\":" + q("Función no encontrada: " + target) + "}";
        StringBuilder b = new StringBuilder("{\"function\":" + fnJson(f) + ",\"instructions\":[");
        InstructionIterator it = currentProgram.getListing().getInstructions(f.getBody(), true);
        int n = 0;
        boolean more = false;
        while (it.hasNext()) {
            Instruction ins = it.next();
            if (n >= limit) { more = true; break; }
            if (n > 0) b.append(',');
            b.append("{\"address\":").append(q(ins.getAddress().toString())).append(",\"text\":").append(q(ins.toString())).append('}');
            n++;
        }
        return b.append("],\"truncated\":").append(more).append('}').toString();
    }

    private String imports(String filter, int limit) {
        StringBuilder b = new StringBuilder("{\"imports\":[");
        int total = 0, shown = 0;
        SymbolIterator it = currentProgram.getSymbolTable().getExternalSymbols();
        while (it.hasNext()) {
            Symbol s = it.next();
            String lib = s.getParentNamespace() != null ? s.getParentNamespace().getName() : "";
            if (!match(s.getName(), filter) && !match(lib, filter)) continue;
            if (shown < limit) {
                if (shown > 0) b.append(',');
                b.append("{\"name\":").append(q(s.getName())).append(",\"library\":").append(q(lib)).append('}');
                shown++;
            }
            total++;
        }
        return b.append("],\"total\":").append(total).append('}').toString();
    }

    private String exports(String filter, int limit) {
        StringBuilder b = new StringBuilder("{\"exports\":[");
        int total = 0, shown = 0;
        SymbolTable st = currentProgram.getSymbolTable();
        AddressIterator it = st.getExternalEntryPointIterator();
        while (it.hasNext()) {
            Address a = it.next();
            Symbol s = st.getPrimarySymbol(a);
            String name = s != null ? s.getName() : null;
            if (!match(name, filter)) continue;
            if (shown < limit) {
                if (shown > 0) b.append(',');
                b.append("{\"address\":").append(q(a.toString())).append(",\"name\":").append(q(name)).append('}');
                shown++;
            }
            total++;
        }
        return b.append("],\"total\":").append(total).append('}').toString();
    }

    private String strings(String filter, int limit) {
        StringBuilder b = new StringBuilder("{\"strings\":[");
        int total = 0, shown = 0;
        Listing listing = currentProgram.getListing();
        DataIterator it = listing.getDefinedData(true);
        while (it.hasNext()) {
            Data d = it.next();
            if (!d.hasStringValue()) continue;
            Object v = d.getValue();
            String text = v != null ? v.toString() : "";
            if (!match(text, filter)) continue;
            if (shown < limit) {
                if (shown > 0) b.append(',');
                b.append("{\"address\":").append(q(d.getAddress().toString())).append(",\"value\":").append(q(text)).append('}');
                shown++;
            }
            total++;
        }
        return b.append("],\"total\":").append(total).append('}').toString();
    }

    private String xrefs(String target, int limit) {
        Address addr = null;
        String hex = target.replaceFirst("^0[xX]", "");
        if (hex.matches("[0-9a-fA-F]+")) {
            try { addr = toAddr(hex); } catch (Exception ignored) { }
        }
        if (addr == null) {
            Function tf = resolveFunction(target);
            if (tf != null) addr = tf.getEntryPoint();
        }
        if (addr == null) return "{\"error\":" + q("Dirección o función no encontrada: " + target) + "}";
        StringBuilder b = new StringBuilder("{\"target\":" + q(addr.toString()) + ",\"references\":[");
        ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(addr);
        int total = 0, shown = 0;
        while (it.hasNext()) {
            Reference r = it.next();
            if (shown < limit) {
                Function from = getFunctionContaining(r.getFromAddress());
                if (shown > 0) b.append(',');
                b.append("{\"from\":").append(q(r.getFromAddress().toString()))
                 .append(",\"type\":").append(q(r.getReferenceType().getName()))
                 .append(",\"function\":").append(q(from != null ? from.getName() : null)).append('}');
                shown++;
            }
            total++;
        }
        return b.append("],\"total\":").append(total).append('}').toString();
    }

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length < 2) throw new IllegalArgumentException("Uso: RuffsExport.java <salida.json> <peticion.properties>");
        try (java.io.Reader r = new java.io.InputStreamReader(new java.io.FileInputStream(a[1]), StandardCharsets.UTF_8)) {
            req.load(r);
        }
        String mode = str("mode");
        String filter = str("filter").toLowerCase();
        String target = str("target");
        String json;
        switch (mode) {
            case "summary": json = summary(); break;
            case "functions": json = functions(filter, num("offset", 0), num("limit", 200)); break;
            case "decompile": json = decompile(target); break;
            case "disassemble": json = disassemble(target, num("limit", 400)); break;
            case "imports": json = imports(filter, num("limit", 500)); break;
            case "exports": json = exports(filter, num("limit", 500)); break;
            case "strings": json = strings(filter, num("limit", 300)); break;
            case "xrefs": json = xrefs(target, num("limit", 200)); break;
            default: json = "{\"error\":" + q("Modo desconocido: " + mode) + "}";
        }
        try (Writer w = new OutputStreamWriter(new FileOutputStream(a[0]), StandardCharsets.UTF_8)) {
            w.write(json);
        }
    }
}

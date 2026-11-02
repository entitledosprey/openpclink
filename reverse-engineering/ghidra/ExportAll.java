import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.io.*;
public class ExportAll extends GhidraScript {
  public void run() throws Exception {
    String out = getScriptArgs()[0];
    DecompInterface d = new DecompInterface();
    d.openProgram(currentProgram);
    try (PrintWriter w = new PrintWriter(new FileWriter(out)); PrintWriter idx = new PrintWriter(new FileWriter(out + ".idx"))) {
      FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
      int n = 0;
      while (it.hasNext() && !monitor.isCancelled()) {
        Function f = it.next();
        idx.println(f.getEntryPoint() + " " + f.getBody().getNumAddresses() + " " + f.getName(true));
        DecompileResults r = d.decompileFunction(f, 60, monitor);
        if (r != null && r.decompileCompleted()) {
          w.println("// ==== " + f.getEntryPoint() + " " + f.getName(true));
          w.println(r.getDecompiledFunction().getC());
        } else {
          w.println("// ==== " + f.getEntryPoint() + " " + f.getName(true) + " DECOMPILE FAILED");
        }
        if (++n % 2000 == 0) println("decompiled " + n);
      }
    }
  }
}

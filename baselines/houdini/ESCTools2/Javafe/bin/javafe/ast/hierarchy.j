// -*- mode: java -*-
/* Copyright 2000, 2001, Compaq Computer Corporation */

/* IF THIS IS A JAVA FILE, DO NOT EDIT IT!  

   Most Java files in this directory which are part of the Javafe AST
   are automatically generated using the astgen comment (see
   ESCTools/Javafe/astgen) from the input file 'hierarchy.h'.  If you
   wish to modify AST classes or introduce new ones, modify
   'hierarchy.j.'
 */

package javafe.ast;

import javafe.util.Assert;
import javafe.util.Location;
import javafe.util.ErrorSet;
import javafe.tc.TagConstants;

// Convention: unless otherwise noted, integer fields named "loc" refer
// to the location of the first character of the syntactic unit

//# EndHeader

/**
 * <code>ASTNode</code> is the root of the abstract syntax tree node
 * hierarchy.  Every class representing a node in the AST is a (direct
 * or indirect) subclass of this class.
 *
 * <p> In designing our AST classes, the following, broad approach was
 * taken:
 *
 * <ul>
 * 
 * <li> <b>Design package javafe.ast to stand alone.</b>
 * 
 * <li> <b>Have a large, deep hierarchy, using the type system to
 * encode many invariants.</b> (Used a program to generate boilerplate
 * code, making it the large hierarchy more manageable.)
 * 
 * <li> <b>Represent children with public fields.</b>
 * 
 * <li> <b>Put most code outside AST classes.</b>
 * 
 * <li> <b>Support both switch-based and visitor-based traversal.</b>
 * 
 * <li> <b>Liberal use of locations.</b> AST classes contain location
 * fields that indicate the corresponding part of the source program.
 * 
 * <li> <b>Use generic decorations.</b>
 * 
 * <li> <b>Rewrite tree to handle disambiguation.</b> Expression names
 * and method names are ambiguous: the be of the form
 * <i>package.type.members</i> or <i>type.members</i> or
 * <i>local-variable.members</i> or even <i>members</i>.  The parser
 * does not attempt to disambiguate among these, but rather generates
 * either <code>ExprName</code> or <code>MethodName</code>.  A later
 * pass replaces these nodes with either <code>VariableAccess</code>
 * or concrete subclasses of <code>FieldAccess</code> or
 * <code>MethodInvocation</code>.
 * 
 * <li> <b>Fill in fields and decorations to handle resolution.</b>
 * The nodes that need to be resolved are: <code>TypeName</code>,
 * <code>VariableAccess</code>, (three subclasses of)
 * <code>FieldAccess</code>, (three subclasses of)
 * <code>MethodInvocation</code>, <code>ConstructorInvocation</code>,
 * and <code>NewInstanceExpr</code>.  All nodes except
 * <code>TypeName</code> have a field named <code>decl</code> of the
 * approriate type that is initialized to <code>null</code> and is
 * filled in by name resolution.  For <code>TypeName</code> we use a
 * decoration rather than a field to avoid a dependency on the
 * <code>javafe.tc</code> package.
 * 
 * </ul>
 * 
 * <p> Measurements (circa SRC ESC/Java):
 * <blockquote>
 * Node abstract classes: 13
 * <br> Node concrete classes: 59
 * <br> Sequence classes: 12
 * <br> Total classes: 84
 * <br>
 * <br> Input to code generator: 842 lines of code (513 excluding comments)
 * <br> Code generator: 993 (675)
 * <br> Total lines of code: 1835 (1188)
 * <br> Output of code generator: 4298
 * </blockquote>
 * 
 * <p> The complete list of subclasses is shown below.  The names of
 * subclasses of this class correspond (in general) to the syntactic
 * units that they represent.  Any exceptions to this rule are
 * documented.  All non-leaf classes are abstract.  In parenthesis,
 * the names and types of fields holding the locally-defined children
 * classes are listed; a postfix <code>*</code> on a type indicates a
 * sequence.  Square brackets indicate optional elements; the only
 * these fields can be null.
 * 
 * <pre>
 * ASTNode
 *    CompilationUnit ([Name pkgName], [LexicalPragma* lexicalPragmas], ImportDecl* imports, TypeDecl* elems)
 *    ImportDecl ()
 *       SingleTypeImport (TypeName typeName)
 *       OnDemandImport (Name pkgName)
 *    TypeDecl (int modifiers, [ModifierPragma* pmodifiers], Identifier id, [TypeModifierPragma* tmodifiers], TypeName* superInterfaces, TypeDeclElem* elems)
 *       ClassDecl ([TypeName superClass])
 *       InterfaceDecl ()
 *    TypeDeclElem () NOTE: This is an <em>interface</em>
 *       TypeDecl
 *       FieldDecl
 *       RoutineDecl (int modifiers, [ModifierPragma* pmodifiers], FormalParaDecl* args, TypeName* raises, [BlockStmt body])
 *          ConstructorDecl ([TypeModifierPragma* tmodifiers])
 *          MethodDecl (Identifier id, Type returnType)
 *       InitBlock (int modifiers, [ModifierPragma* pmodifiers], BlockStmt block)
 *       TypeDeclElemPragma ()
 *    GenericVarDecl (int modifiers, [ModifierPragma* pmodifiers], Identifier id, Type type)
 *       LocalVarDecl ([VarInit init])
 *       FieldDecl ([VarInit init])
 *       FormalParaDecl ()
 *    Stmt ()
 *       GenericBlockStmt (Stmt* stmts)
 *          BlockStmt ()
 *          SwitchStmt (Expr expr)
 *       AssertStmt (Expr expr, String l)
 *       VarDeclStmt (LocalVarDecl decl)
 *       ClassDeclStmt (ClassDecl anonDecl)
 *       WhileStmt (Expr expr, Stmt stmt)
 *       DoStmt (Expr expr, Stmt stmt)
 *       SynchronizeStmt (Expr expr, BlockStmt stmt)
 *       EvalStmt (Expr expr)
 *       ReturnStmt ([Expr expr])
 *       ThrowStmt (Expr expr)
 *       BranchStmt ([Identifier label])
 *          BreakStmt ()
 *          ContinueStmt ()
 *       LabelStmt (Identifier label, Stmt stmt)
 *       IfStmt (Expr expr, Stmt thn, Stmt els)
 *       ForStmt (Stmt* forInit, Expr test, Expr* forUpdate, Stmt body)
 *       SkipStmt ()
 *       SwitchLabel ([Expr expr])
 *       TryFinallyStmt (Stmt tryClause, Stmt finallyClause)
 *       TryCatchStmt (Stmt tryClause, CatchClause* catchClauses)
 *       StmtPragma ()
 *       ConstructorInvocation (boolean superCall, [Expr enclosingInstance], Expr* args)
 *    CatchClause (FormalParaDecl arg, Stmt body)
 *    VarInit ()
 *       ArrayInit (VarInit* elems)
 *       Expr ()
 *          ThisExpr (Type classPrefix)
 *          LiteralExpr (int tag, [Object value])
 *          ArrayRefExpr (Expr array, Expr index)
 *          NewInstanceExpr ([Expr enclosingInstance], TypeName type, Expr* args, [ClassDecl decl])
 *          NewArrayExpr (Type type, Expr* dims, [ArrayInit init])
 *          CondExpr (Expr test, Expr thn, Expr els)
 *          InstanceOfExpr (Expr expr, Type type)
 *          CastExpr (Expr expr, Type type)
 *          BinaryExpr (int op, Expr left, Expr right)
 *          UnaryExpr (int op, Expr expr)
 *          ParenExpr (Expr expr)
 *          AmbiguousVariableAccess (Name name)
 *          VariableAccess (Identifier id)
 *          FieldAccess (ObjectDesignator od, Identifier id)
 *          AmbiguousMethodInvocation (Name name, Expr* args)
 *          MethodInvocation (ObjectDesignator od, Identifier id, Expr* args)
 *          ClassLiteral (Type type)
 *    ObjectDesignator ()  // "expr.", "type." or "super."
 *       ExprObjectDesignator (Expr expr)
 *       TypeObjectDesignator (Type type)
 *       SuperObjectDesignator ()
 *    Type ()
 *       ErrorType() // was previously represented as a PrimitiveType
 *       PrimitiveType (int tag)
 *         JavafePrimitiveType  ()  // this is a new type introduced to allow for other (e.g., esc) primitive types
 *       TypeName (Name name)
 *       ArrayType (Type elemType)
 *    Name ()
 *       SimpleName (Identifier id)
 *       CompoundName (Identifier* ids)
 *    ModifierPragma ()
 *    LexicalPragma ()
 *    TypeModifierPragma ()
 * </pre>
 */

public abstract class ASTNode implements Cloneable
{
  /**
   * The decorations that have been attached to this node.  This is a
   * package-level field accessed by the <code>ASTDecoration</code>
   * class.  There are different ways to implement decorations; this
   * way is not space-efficient, but it's pretty fast.
   */

  //@ invariant decorations != null ==> (\typeof(decorations) == \type(Object[]));
  /*@spec_public*/ private /*@nullable*/ Object[] decorations;  

    public int bogusField;
    //@ public model int startLoc;
    //@ public model boolean isInternal;
    //@ public represents isInternal <- startLoc == Location.NULL;

    /*@ public normal_behavior
      @ ensures \result == startLoc;
      @*/
    public /*@ pure @*/ abstract int getStartLoc();

    public /*@ pure @*/ int getEndLoc() { 
	return getStartLoc();
    }

    //@ public model int endLoc;
    //@ public represents endLoc <- getEndLoc();
    //+@ public invariant startLoc == Location.NULL <==> endLoc == Location.NULL;    

  /** returns true iff this in an internally declared node. Note
   * that internally declared types have no associated location.
   */
  /*@ public normal_behavior
    @ ensures \result == isInternal;
    @*/
    public /*@ pure @*/ boolean isInternal() {
	return getStartLoc() == Location.NULL;
    }

  //@ ensures \typeof(\result) == \typeof(this);
  //@ ensures \result.owner == null;
  public /*@ non_null */ Object clone(boolean b)  {
    ASTNode n = null;
    try {
      n = (ASTNode)super.clone();
      if (!b) {
        n.decorations = null;
      }
    } catch(java.lang.CloneNotSupportedException e) {
      ErrorSet.fatal("Internal error in AST hierarchy: no clone");
      n = this; // dummy assingment to appease escjava.
    }
    return n;
  }
 
  /**
   * Clone node, where the clone has the same decorations as original.
   */
  public /*@non_null*/ Object clone() {
    return clone(true);
  }
  
  public /*@nullable*/ Object[] getDecorations() {
    return decorations;
  }

  //@ requires d != null ==> \typeof(d) == \type(Object[]);
  public void setDecorations(/*@nullable*/ Object d[]) {
    decorations = d;
  }
    
  //$$
  static public int dotCounter = 0 ;
  public int dotId;

    /*
     * Constructor needed for GRAPHICAL representation (dot output) 
     *
     * This is temporary as I don't know how the class generator works.
     * And it always generate a constructor empty (which classes with this one
     * if I remove this useless boolean).
     */
//   protected ASTNode(){
//     dotCounter += 1;
//     dotId = dotCounter;
//   }    
  //$$

}

/* ---------------------------------------------------------------------- */

/** The <code>make</code> method of this class has the side effect of
pointing the <code>parent</code> pointers of the <code>TypeDecl</code>s
inside a <code>CompilationUnit</code> to point to that unit. */

public class CompilationUnit extends ASTNode
{
  //# Name pkgName NullOK		// null if in unnamed package
  //# LexicalPragma* lexicalPragmas NullOK
  //# ImportDecl* imports
  //# TypeDecl* elems NoCheck
  //# int loc NotNullLoc
  //# TypeDeclElem* otherPragmas NoCheck

  //@ public represents startLoc <- loc;

  public boolean duplicate = false;

  //# PostCheckCall
  private void postCheck() {
    for(int i = 0; i < elems.size(); i++) {
      for(int j = i+1; j < elems.size(); j++)
	Assert.notFalse(elems.elementAt(i) != elems.elementAt(j));  //@ nowarn Pre;
    }
  }

  /**
   * @return true iff this CompilationUnit was created from a .class
   * file.
   */
  public boolean isBinary() {
    return Location.toFileName(loc).endsWith(".class");
  }

  public /*@ pure @*/ int getStartLoc() { return loc; }
  public /*@ pure @*/ int getEndLoc() { 
    if (elems == null || elems.size() < 1)
      return super.getEndLoc();

    return elems.elementAt(elems.size()-1).getEndLoc();
  }

  public /*@non_null*/ javafe.genericfile.GenericFile sourceFile() {
    return Location.toFile(loc);
  }
}

/* ---------------------------------------------------------------------- */

public abstract class ImportDecl extends ASTNode
{
  //@ public represents startLoc <- loc;

  //# int loc NotNullLoc
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

public class SingleTypeImportDecl extends ImportDecl
{
  //# TypeName typeName
}

public class OnDemandImportDecl extends ImportDecl
{
  //# Name pkgName
}

/* ---------------------------------------------------------------------- */

/**
 * Represents a TypeDeclaration.
 * Common fields of ClassDeclarations and InterfaceDeclarations are here.
 */

public abstract class TypeDecl extends ASTNode implements TypeDeclElem
{
  /**
   * If specOnly is true, then this is only a spec.  In particular,
   * methods do not have non-empty bodies, no initializers are present
   * for fields, and no static bodies are present.
   */
  public boolean specOnly = false;

  //# int modifiers
  //# ModifierPragma* pmodifiers NullOK
  //# Identifier id NoCheck
  //# TypeName* superInterfaces

  //# TypeModifierPragma* tmodifiers NullOK

  //* "invariant" elems.<each element>.hasParent is true
  //# TypeDeclElem* elems NoCheck

  //# int loc NotNullLoc
  //# int locId NotNullLoc
  //# int locOpenBrace NotNullLoc
  //# int locCloseBrace NotNullLoc

  //@ invariant hasParent ==> parent != null;
  public /*@nullable*/TypeDecl parent;

  public TypeDecl getParent() { return parent; }
  public void setParent(/*@non_null*/ TypeDecl p) { parent = p; }

  public int getModifiers() { return modifiers; }
  public void setModifiers(int m) { modifiers = m; }
  public ModifierPragmaVec getPModifiers() { return pmodifiers; }

  //# PostCheckCall
  private void postCheck() {
    // check invariants on modifiers...
    for(int i = 0; i < elems.size(); i++) {
      Assert.notFalse(elems.elementAt(i).getParent() == this);	//@ nowarn Pre;
      for(int j = i+1; j < elems.size(); j++)
	Assert.notFalse(elems.elementAt(i) != elems.elementAt(j));  //@ nowarn Pre;
    }
  }

    /**
     * @return true iff this TypeDecl was created from a .class
     * file.
     *
     * precondition: We have already been associated with a TypeSig.
     */
    public boolean isBinary() {
        javafe.tc.TypeSig sig = javafe.tc.TypeSig.getSig(this);
	CompilationUnit cu = sig.getCompilationUnit();

	return cu.isBinary();
    }

  //@ public represents startLoc <- loc;

  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == locCloseBrace;
  public /*@ pure @*/ int getEndLoc() { return locCloseBrace; }
}

public class ClassDecl extends TypeDecl
{
  //# TypeName superClass NullOK

  //# PostMakeCall

  /** Set the parent pointer of the <code>TypeDeclElem</code>s
    inside the <code>this</code>. */
  private void postMake() {
    for(int i = 0; i < elems.size(); i++)
      elems.elementAt(i).setParent(this);
  }
}

public class InterfaceDecl extends TypeDecl
{
  //# PostMakeCall

  /** Set the parent pointer of the <code>TypeDeclElems</code>s
    inside the <code>this</code>. */
  private void postMake() {
    for(int i = 0; i < elems.size(); i++)
      elems.elementAt(i).setParent(this);
  }
}

/* ---------------------------------------------------------------------- */

/** Represents both MethodDeclarations and ConstructorDeclarations.
 */

public abstract class RoutineDecl extends ASTNode implements TypeDeclElem
{
  //@ invariant hasParent ==> parent != null;
  public /*@nullable*/TypeDecl parent;

  public boolean binaryArgNames = false; // true if the ids of formal
	// arguments are binary manufactured names instead of source code names

  public boolean implicit = false;
  public boolean specOnly = false;
  public TypeNameVec originalRaises = null;

  //# int modifiers
  //# ModifierPragma* pmodifiers NullOK
  //# TypeModifierPragma* tmodifiers NullOK
  //# FormalParaDecl* args
  //# TypeName* raises
  //# BlockStmt body NullOK
  //# int locOpenBrace
  //@ invariant body != null ==> locOpenBrace != Location.NULL;
  //# int loc NotNullLoc
  //# int locId NotNullLoc

  // The "locThrowsKeyword" field should be used only if "raises" denotes
  // a nonempty list, in which case "locThrowsKeyword" is not "Location.NULL"
  //# int locThrowsKeyword

  //# PostCheckCall
  private void postCheck() {
    // Check invariants on modifiers...

    for(int i = 0; i < args.size(); i++)
      // Check invariants on modifiers...
      /*skip*/;
  }

  public TypeDecl getParent() { return parent; }
  public void setParent(/*@non_null*/ TypeDecl p) { parent = p; }

  public int getModifiers() { return modifiers; }
  public void setModifiers(int m) { modifiers = m; }
  public ModifierPragmaVec getPModifiers() { return pmodifiers; }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }

  abstract public Identifier id();

  public /*@ pure @*/ int getEndLoc() { 
      if (body == null)
	  return super.getEndLoc();

      return body.getEndLoc();
  }
  private Type[] argtypes = null;
  public Type[] argTypes() {
	if (argtypes != null) return argtypes;
	argtypes = new Type[args.size()];
	for (int i=0; i<args.size(); ++i) {
		argtypes[i] = args.elementAt(i).type;
	}
	return argtypes;
  }
}

/** Represents a ConstructorDeclaration.
 *
 *  The first Stmt in the body field should be a ConstructorInvocation. If
 *  the source code does not contain a constructor invocation, it is
 *  inserted by the parser.
 */

public class ConstructorDecl extends RoutineDecl
{
  //# MakerSpec requires body != null ==> locOpenBrace != Location.NULL;

  public Identifier id() { return parent != null ? parent.id : Identifier.intern("<constructor>"); }
}

public class MethodDecl extends RoutineDecl
{
  //# Identifier id NoCheck
  //# Type returnType Syntax
  //# int locType NotNullLoc

  //# MakerSpec requires body != null ==> locOpenBrace != Location.NULL;

  public Identifier id() { return id; }
}

/** Represents an initializing block of code as a class member
 *
 *  We include modifiers for later extensibility to JDK 1.1, where both
 *  static and dynamic initializer blocks are allowed.
 */

public class InitBlock extends ASTNode implements TypeDeclElem
{
  //@ invariant hasParent ==> parent != null;
  public /*@nullable*/TypeDecl parent;

  //# int modifiers
  //# ModifierPragma* pmodifiers NullOK
  //# BlockStmt block

  public TypeDecl getParent() { return parent; }
  public void setParent(/*@non_null*/ TypeDecl p) { parent = p; }

  public int getModifiers() { return modifiers; }
  public void setModifiers(int m) { modifiers = m; }
  public ModifierPragmaVec getPModifiers() { return pmodifiers; }

  //@ public represents startLoc <- block.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return block.getStartLoc(); }
  public /*@ pure @*/ int getEndLoc() { return block.getEndLoc(); }
}

public abstract class TypeDeclElemPragma
         extends ASTNode implements TypeDeclElem
{
  /* denotes that a pragma is redundant (e.g. "invariant_redundantly") */
  //# boolean redundant

  //@ invariant hasParent ==> parent != null;
  public /*@nullable*/TypeDecl parent;

  public TypeDecl getParent() { return parent; }
  public void setParent(/*@non_null*/ TypeDecl p) { parent = p; }
  public void decorate(ModifierPragmaVec modifierPragmas) {}
  protected TypeDeclElemPragma() {}

  abstract public int getTag();
  public int getModifiers() { return 0; }
  public void setModifiers(int m) {}
  public ModifierPragmaVec getPModifiers() { return null; }
  public boolean isRedundant() { return redundant; }
  public void setRedundant(boolean v) { redundant = v; }
}

/* ---------------------------------------------------------------------- */

/** Represents all variable declarations, including field declarations,
 *  local variables and formal arguments.
 *
 *  We 'unroll' field and variable decls, 
 *  so "<code>int x,y;</code>" becomes "<code>int x; int y;</code>".
 *  This unrolling can be detected by looking for sequential
 *  <code>VarDecl</code>s whose <code>Type</code> fields have the same
 *  starting location.
 */

public abstract class GenericVarDecl extends ASTNode
{
  //# int modifiers
  //# ModifierPragma* pmodifiers NullOK
  //# Identifier id NoCheck
  //# Type type
  //# int locId

  //xxx  public boolean isInternal() { return locId == javafe.util.Location.NULL; }

  //@ public represents startLoc <- type.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return type.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == type.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return type.getEndLoc(); }
  public int getModifiers() { return modifiers; }
  public void setModifiers(int m) { modifiers = m; }

  protected GenericVarDecl(int modifiers, ModifierPragmaVec pmodifiers, /*@ non_null @*/ Identifier id, /*@ non_null @*/ Type type) {
     super();
     this.modifiers = modifiers;
     this.pmodifiers = pmodifiers;
     this.id = id;
     this.type = type;
     this.locId = Location.NULL;
  }
}

/** Represents a LocalVariableDeclarationStatement.
 *  The modifiers field of LocalVarDecl allow for future extensibility.
 */

public class LocalVarDecl extends GenericVarDecl
{
  //# VarInit init NullOK
  //@ invariant !isInternal();

  // The "locAssignOp" field is used only if "init" is non-null
  //# int locAssignOp
  
  // used to track where the variable came from in DSA
  public GenericVarDecl source;

  //# PostCheckCall
  private void postCheck() {
    // Check invariants on modifiers...
    // should be liberal...
  }

  public /*@ pure @*/ int getEndLoc() {
    if (init == null)
      return super.getEndLoc();

    return init.getEndLoc();
  }
}

/** Represents a field declaration. */

public class FieldDecl extends GenericVarDecl implements TypeDeclElem
{
  //@ invariant hasParent ==> parent != null;
  public /*@nullable*/TypeDecl parent;

  //# VarInit init NullOK

  // The "locAssignOp" field is used only if "init" is non-null
  //# int locAssignOp

  //# PostCheckCall
  private void postCheck() {
    // Check invariants on modifiers...
    // should be liberal...
  }

  public TypeDecl getParent() { return parent; }
  public void setParent(/*@non_null*/ TypeDecl p) { parent = p; }
  public ModifierPragmaVec getPModifiers() { return null; }

  public /*@ pure @*/ int getEndLoc() {
    if (init == null)
      return super.getEndLoc();

    return init.getEndLoc();
  }
  protected FieldDecl(int modifiers, ModifierPragmaVec pmodifiers, /*@ non_null @*/ Identifier id, /*@ non_null @*/ Type type, VarInit init)
  {
     super(modifiers, pmodifiers, id, type);
     this.init = init;
     this.locAssignOp = Location.NULL;
  }

  //@ requires (type instanceof PrimitiveType);
  public static FieldDecl makeInternal(int modifiers, ModifierPragmaVec pmodifiers, /*@ non_null @*/ Identifier id, /*@ non_null @*/ Type type, VarInit init) {
     FieldDecl result = new FieldDecl(modifiers, pmodifiers, id, type, init);
     return result;
  }
}

/**
 * Represents a FormalParameter. 
 */

public class FormalParaDecl extends GenericVarDecl
{
  //@ invariant !isInternal();
}

/* ---------------------------------------------------------------------- */

/** Represents a BlockStatement syntactic unit (which includes
 * variable declarations). */
public abstract class Stmt extends ASTNode
{ 
	
}

// Abstract class containing common parts of Block and SwitchBlock
// syntactic units.

public abstract class GenericBlockStmt extends Stmt
{
  //# Stmt* stmts
  //# int locOpenBrace NotNullLoc
  //# int locCloseBrace NotNullLoc

  public /*@ pure @*/ int getEndLoc() { return locCloseBrace; }
}

public class BlockStmt extends GenericBlockStmt
{
  //# PostCheckCall
  private void postCheck() {
    for(int i = 0; i < stmts.size(); i++) {
      int t = stmts.elementAt(i).getTag();
      Assert.notFalse(t != TagConstants.SWITCHLABEL);	//@ nowarn Pre;
      Assert.notFalse(i == 0				//@ nowarn Pre;
	|| t != TagConstants.CONSTRUCTORINVOCATION);
    }
  }
  //@ public represents startLoc <- locOpenBrace;
  public /*@ pure @*/ int getStartLoc() { return locOpenBrace; }
}

public class SwitchStmt extends GenericBlockStmt
{
  //# Expr expr
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

public class AssertStmt extends Stmt
{
  //# Expr pred
  //# Expr label NoCheck NullOK
  //# int loc NotNullLoc
  public IfStmt ifStmt;

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  public /*@ pure @*/ int getEndLoc() { 
        return (label == null ? pred.getEndLoc() : label.getEndLoc());
  }
}

public class VarDeclStmt extends Stmt
{
  //# LocalVarDecl decl

  //@ public represents startLoc <- decl.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return decl.getStartLoc(); }

  //@ also public normal_behavior
  //@ ensures \result == decl.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return decl.getEndLoc(); }
}

public class ClassDeclStmt extends Stmt
{
  //# ClassDecl decl

  //# PostCheckCall
  private void postCheck() {
  // Awaiting information from Sun...
  }

  //@ public represents startLoc <- decl.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return decl.getStartLoc(); }

  //@ also public normal_behavior
  //@ ensures \result == decl.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return decl.getEndLoc(); }
}

public class WhileStmt extends Stmt
{
  //# Expr expr
  //# Stmt stmt
  //# int loc NotNullLoc
  //# int locGuardOpenParen NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = stmt.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == stmt.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return stmt.getEndLoc(); }
}

public class DoStmt extends Stmt
{
  //# Expr expr
  //# Stmt stmt
  //# int loc NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = stmt.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }
  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == expr.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return expr.getEndLoc(); }
}

public class SynchronizeStmt extends Stmt
{
  //# Expr expr
  //# BlockStmt stmt
  //# int loc NotNullLoc
  //# int locOpenParen NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == stmt.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return stmt.getEndLoc(); }
}

public class EvalStmt extends Stmt
{
  //# Expr expr

  //@ public represents startLoc <- expr.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return expr.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == expr.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return expr.getEndLoc(); }
}

public class ReturnStmt extends Stmt
{
  //# Expr expr NullOK
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  public /*@ pure @*/ int getEndLoc() {
    if (expr == null)
      return super.getEndLoc();

    return expr.getEndLoc();
  }
}

public class ThrowStmt extends Stmt
{
  //# Expr expr
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == expr.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return expr.getEndLoc(); }
}

public abstract class BranchStmt extends Stmt
{
  //# Identifier label NoCheck NullOK
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

public class BreakStmt extends BranchStmt
{ }

public class ContinueStmt extends BranchStmt
{ }

public class LabelStmt extends Stmt
{
  //# Identifier label NoCheck
  //# Stmt stmt
  //# int locId NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = stmt.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }
  //@ public represents startLoc <- locId;
  public /*@ pure @*/ int getStartLoc() { return locId; }
  //@ also public normal_behavior
  //@ ensures \result == stmt.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return stmt.getEndLoc(); }
}

public class IfStmt extends Stmt
{
  //# Expr expr
  //# Stmt thn
  //# Stmt els
  //# int loc NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = thn.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL 	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
    t = els.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  public /*@ pure @*/ int getEndLoc() { 
    int thenL = thn.getEndLoc();
    int elseL = els.getEndLoc();
    return Math.max(thenL, elseL);
  }
}

/** Represents a ForStatement.
 *
 *  The ForInit part of a ForStatement is either a StatementExpressionList
 *  or a LocalVariableDeclaration. Both alternatives are represented here
 *  by a Stmt sequence. 
 *  Note that our Stmt class corresponds to a BlockStatement syntactic unit, 
 *  so <code>forInit</code> can contain variable declarations.
 */

public class ForStmt extends Stmt
{
  //# Stmt* forInit
  //# Expr test
  //# Expr* forUpdate
  //# Stmt body
  //# int loc NotNullLoc
  //# int locFirstSemi NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    for(int i = 0; i < forInit.size(); i++) {
      int t = forInit.elementAt(i).getTag();
      Assert.notFalse(t != TagConstants.SWITCHLABEL	 //@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION);
      // Could have a stronger invariant here...
    }
    // Invariants on forUpdate??...
    int t = body.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }
  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == body.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return body.getEndLoc(); }
}

public class SkipStmt extends Stmt
{
  //# int loc NotNullLoc
  
  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

/**
 * Represents a SwitchLabel syntactic unit.  If expr is null, then
 * represents the "default" label, otherwise represents a case
 * label.
 *
 * <p> We infer a default: break at the end of each switch statement
 * that does not contain a default case to simplify translation.  Such
 * inferred cases will have their location equal to the location of
 * the closing bracket of the switch statement they are contained in.
 */

public class SwitchLabel extends Stmt
{
  //# Expr expr NullOK
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

public class TryFinallyStmt extends Stmt
{
  // @note locTry might will be shared with tryClause.locTry if
  // tryClause is a TryCatchStmt
  //# Stmt tryClause
  //# Stmt finallyClause
  //# int loc NotNullLoc
  //# int locFinally NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = tryClause.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
    t = finallyClause.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == finallyClause.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return finallyClause.getEndLoc(); }
}

/**
 * Represents a try-catch statement.
 */

public class TryCatchStmt extends Stmt
{
  //# Stmt tryClause
  //# CatchClause* catchClauses
  //# int loc NotNullLoc

  //# PostCheckCall
  private void postCheck() {
    int t = tryClause.getTag();
    Assert.notFalse(t != TagConstants.SWITCHLABEL	//@ nowarn Pre;
		    && t != TagConstants.CONSTRUCTORINVOCATION
		    && t != TagConstants.VARDECLSTMT);
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  public /*@ pure @*/ int getEndLoc() { 
    if (catchClauses == null || catchClauses.size()<1)
      return tryClause.getEndLoc();

    return catchClauses.elementAt(catchClauses.size()-1).getEndLoc();
  }
}

public abstract class StmtPragma extends Stmt
{
  /* denotes that a pragma is redundant (e.g. "assume_redundantly") */
  //# boolean redundant

  public boolean isRedundant() { return redundant; }
  public void setRedundant(boolean v) { redundant = v; }
  // when there are various synonomus tag names, tag and getTag() will be
  // a canonical value; originalTag will be the specific value.  Thus tag
  // can be used in switch statements and originalTag for printing. 
  private int originalTag;
  public StmtPragma setOriginalTag(int t) { originalTag = t; return this; }
  public int originalTag() {
    return (originalTag == 0) ? getTag() : originalTag;
  }
  protected StmtPragma() {}
}

/**
 * Represents an ExplicitConstructorInvocation. 
 * <p> Only occurs as the first Stmt in a ConstructorDecl. 
 * <p> Name resolution sets the decl field to the callee.
 */

public class ConstructorInvocation extends Stmt
{
  /**
   * superCall is true implies call is "super(...)",
   * superCall is false implies call is "this(...)".
   */
  //# boolean superCall

  /**
   * The enclosing instance is the object expression before a super
   * call ( <enclosingInstance>.super(...) ).  This field may be null
   * if there is no such expression.
   *
   * @note If the supertype in question is an inner class, then the
   * type checker will infer a [<C>.]this expression if no expression
   * is present and place it in this slot.  (See ThisExpr for how to
   * distinguish inferred this expressions.)<p>
   */
  //# Expr enclosingInstance NullOK

  //@ invariant enclosingInstance != null && superCall ==> locDot != Location.NULL;
  //# int locDot

  //# int locKeyword NotNullLoc
  //# int locOpenParen NotNullLoc

  //# Expr* args

  public ConstructorDecl decl;

  //# PostCheckCall
  private void postCheck() {
    if (decl != null) {
      // Any invariants here...???
    }
  }

  /*@ public represents startLoc <- (enclosingInstance == null) 
    @		? locKeyword : enclosingInstance.getStartLoc();
    @*/
  public /*@ pure @*/ int getStartLoc() {
    if (enclosingInstance == null) return locKeyword;
    else return enclosingInstance.getStartLoc();
  }

  public /*@ pure @*/ int getEndLoc() { 
    if (args.size()<1)
      return super.getEndLoc();

    return args.elementAt(args.size()-1).getEndLoc();
  }

  //# NoMaker
  //@ requires superCall ==> locDot != Location.NULL;
  //@ requires locKeyword != javafe.util.Location.NULL;
  //@ requires locOpenParen != javafe.util.Location.NULL;
  public static /*@non_null*/ ConstructorInvocation make(boolean superCall, 
                                           /*@non_null*/ Expr enclosingInstance, 
                                           int locDot, 
                                           int locKeyword, 
                                           int locOpenParen, 
                                           /*@ non_null @*/ ExprVec args) {
     ConstructorInvocation result = new ConstructorInvocation(
                                          superCall,
                                          enclosingInstance,
                                          locDot,
                                          locKeyword,
                                          locOpenParen,
                                          args);
     return result;
  }
}

/* ---------------------------------------------------------------------- */

public class CatchClause extends ASTNode
{
  //# FormalParaDecl arg
  //# BlockStmt body
  //# int loc NotNullLoc

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == body.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return body.getEndLoc(); }
}

/* ---------------------------------------------------------------------- */

/**
 * Represents a VariableInitializer. 
 */

public abstract class VarInit extends ASTNode
{ }

/**
 * Represents an ArrayInitializer. 
 */

public class ArrayInit extends VarInit
{
  //# VarInit* elems
  //# int locOpenBrace NotNullLoc
  //# int locCloseBrace NotNullLoc

  //@ public represents startLoc <- locOpenBrace;
  public /*@ pure @*/ int getStartLoc() { return locOpenBrace; }
  //@ also public normal_behavior
  //@ ensures \result == locCloseBrace;
  public /*@ pure @*/ int getEndLoc() { return locCloseBrace; }
}

/**
 * Represents an Expression.
 *
 * We define Expr as a subclass of VarInit to yield a more compact
 * representation. An alternative is for VarInit to contain a pointer
 * to an Expr, which would result in a number of additional pointers,
 * in particular for array initializers.
 */

public abstract class Expr extends VarInit
{ }

/**
 * We represent [C.]this.
 *
 * <p> classPrefix holds C if present, and null otherwise.
 *
 * @note classPrefix will hold a <em>**TypeSig**</em> instead of a
 * TypeName if we have been inferred.  (The inferred field specifies
 * whether or not we have been inferred.)
 */

public class ThisExpr extends Expr
{
  //# Type classPrefix NullOK

  //# int loc NotNullLoc

  //* Were we inferred by the disambiguation code?
  public boolean inferred = false;

  /*@ public represents startLoc <- (classPrefix != null && classPrefix instanceof TypeName)
    @ 		? classPrefix.getStartLoc() : loc;
    @*/
    public /*@ pure @*/ int getStartLoc() {
        if (classPrefix != null && classPrefix instanceof TypeName)
	    return classPrefix.getStartLoc();
	return loc;
    }

  //@ also public normal_behavior
  //@ ensures \result == Location.inc(loc, 3);
  public /*@ pure @*/ int getEndLoc() { return Location.inc(loc, 3); }
}

/**
 * Represents a Literal.
 *
 * <p> The tag of a LiteralExpr should be one of the *LIT (eg INTLIT)
 * constants defined in TagConstants.  The value fields is a
 * Character/String/Long/Double/Boolean/null, as appropriate.
 */

public class LiteralExpr extends Expr
{
  //@ invariant isValidTag(tag);
  //# ManualTag
  //# int tag

  //@ invariant isValidValue(tag, value);
  //# Object value NullOK NoCheck

  final
  //# int loc 
  public final int getTag() { return this.tag; }

  //# PostCheckCall
  private void postCheck() {
    Assert.notFalse(isValidTag(tag));
    Assert.notFalse(isValidValue(tag, value));
  }
  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  //@ also public normal_behavior
  //@ ensures \result == loc;
  public /*@ pure @*/ int getEndLoc() { return loc; }

  static public LiteralExpr cast(LiteralExpr lit, int t) {
	if (!(lit.value instanceof Number)) return lit;
	Number num = (Number)lit.value;
	if (t == TagConstants.BYTELIT) {
	    return LiteralExpr.make(t,new Integer(num.byteValue()),lit.getStartLoc());
	} else if (t == TagConstants.SHORTLIT) {
	    return LiteralExpr.make(t,new Short(num.shortValue()),lit.getStartLoc());
	} else if (t == TagConstants.INTLIT) {
	    return LiteralExpr.make(t,new Integer(num.intValue()),lit.getStartLoc());
	} else if (t == TagConstants.LONGLIT) {
	    return LiteralExpr.make(t,new Long(num.longValue()),lit.getStartLoc());
	} else if (t == TagConstants.FLOATLIT) {
	    return LiteralExpr.make(t,new Float(num.floatValue()),lit.getStartLoc());
	} else if (t == TagConstants.DOUBLELIT) {
	    return LiteralExpr.make(t,new Double(num.doubleValue()),lit.getStartLoc());
	} else return lit;
	// FIXME - what about casts to character values ???
  }

  // Note: Java does not actually have byte and short literals.
  // We include them here because we pre-compute literals that have been
  // cast to other types, e.g. (short)0, as a literal of the target type
  // FIXME - short and byte are included for completeness, but I'm not sure they actually ever get used that way

     /*@ ensures \result <==> tag==TagConstants.NULLLIT    ||
       @                      tag==TagConstants.BOOLEANLIT ||
       @                      tag==TagConstants.BYTELIT    ||
       @                      tag==TagConstants.SHORTLIT   ||
       @                      tag==TagConstants.CHARLIT    ||
       @                      tag==TagConstants.INTLIT     ||
       @                      tag==TagConstants.LONGLIT    ||
       @                      tag==TagConstants.FLOATLIT   ||
       @                      tag==TagConstants.DOUBLELIT  ||
       @                      tag==TagConstants.STRINGLIT;
       @*/
      public static boolean isValidTag(int tag)
      {
          return tag==TagConstants.NULLLIT    ||
                 tag==TagConstants.BOOLEANLIT ||
                 tag==TagConstants.BYTELIT    ||
                 tag==TagConstants.SHORTLIT   ||
                 tag==TagConstants.CHARLIT    ||
                 tag==TagConstants.INTLIT     ||
                 tag==TagConstants.LONGLIT    ||
                 tag==TagConstants.FLOATLIT   ||
                 tag==TagConstants.DOUBLELIT  ||
                 tag==TagConstants.STRINGLIT;
      }
     /*@ requires isValidTag(tag);
       @ ensures \result <==> (tag==TagConstants.NULLLIT    ==> value == null) &&
       @                      (tag==TagConstants.BOOLEANLIT ==> value instanceof Boolean) &&
       @                      (tag==TagConstants.BYTELIT    ==> value instanceof Byte) &&
       @                      (tag==TagConstants.SHORTLIT   ==> value instanceof Short) &&
       @                      (tag==TagConstants.CHARLIT    ==> value instanceof Integer) &&
       @                      (tag==TagConstants.INTLIT     ==> value instanceof Integer) &&
       @                      (tag==TagConstants.LONGLIT    ==> value instanceof Long) &&
       @                      (tag==TagConstants.FLOATLIT   ==> value instanceof Float) &&
       @                      (tag==TagConstants.DOUBLELIT  ==> value instanceof Double) &&
       @                      (tag==TagConstants.STRINGLIT  ==> value instanceof String);
       @*/
      public static boolean isValidValue(int tag, Object value)
      {
          if (tag == TagConstants.NULLLIT)    return value == null;
          if (tag == TagConstants.BOOLEANLIT) return value instanceof Boolean;
          if (tag == TagConstants.BYTELIT)    return value instanceof Byte;
          if (tag == TagConstants.SHORTLIT)   return value instanceof Short;
          if (tag == TagConstants.CHARLIT)    return value instanceof Integer;
          if (tag == TagConstants.INTLIT)     return value instanceof Integer;
          if (tag == TagConstants.LONGLIT)    return value instanceof Long;
          if (tag == TagConstants.FLOATLIT)   return value instanceof Float;
          if (tag == TagConstants.DOUBLELIT)  return value instanceof Double;
          if (tag == TagConstants.STRINGLIT)  return value instanceof String;
	  return false; //should be unreachable
      } 

  //# NoMaker
  //@ requires isValidTag(tag);
  //@ requires isValidValue(tag, value);
  //@ requires loc != javafe.util.Location.NULL;
  public static /*@non_null*/ LiteralExpr make(int tag, Object value, int loc) {
     LiteralExpr result = new LiteralExpr(tag,value,loc);
     return result;
  }

  //@ requires isValidTag(tag);
  //@ requires isValidValue(tag, value);
  public static /*@non_null*/ LiteralExpr makeNonSyntax(int tag, Object value) {
     LiteralExpr result = new LiteralExpr(tag,value,Location.NULL);
     return result;
  }

  public /*@ non_null @*/ String getInfoNewTree(){
	  return (value==null ? "null" : value.toString());
  }
}

public class ArrayRefExpr extends Expr
{
  //# Expr array
  //# Expr index
  //# int locOpenBracket NotNullLoc
  //# int locCloseBracket NotNullLoc

  //@ public represents startLoc <- array.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return array.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == locCloseBracket;
  public /*@ pure @*/ int getEndLoc() { return locCloseBracket; }
}

public class NewInstanceExpr extends Expr
{
  /**
   * The enclosing instance is the object expression before a new
   * call ( <enclosingInstance>.new T(...) ).  This field may be null
   * if there is no such expression.
   *
   * @note If the type in question is an inner class, then the
   * type checker will infer a [<C>.]this expression if no expression
   * is present and place it in this slot.  (See ThisExpr for how to
   * distinguish inferred this expressions.)<p>
   */
  //# Expr enclosingInstance NullOK

  //@ invariant (enclosingInstance == null) == (locDot==Location.NULL);
  //# int locDot

  //# TypeName type
  //# Expr* args

  /**
   * If the new expression includes a declaration of an inner class,
   * then "anonDecl" will be non-null.  In this case, the
   * "superclass" field of "ananDecl" will be null and
   * "superinterfaces" list of "anonDecl" will be empty.  One of
   * these fields needs to be modified during type checking depending
   * on whether "type" is a class or an interface.
   */
  //# ClassDecl anonDecl NullOK

  //# int loc NotNullLoc
  //# int locOpenParen NotNullLoc

  public ConstructorDecl decl;

  /*@ public represents startLoc <- 
    @ 		(enclosingInstance == null) ? loc : enclosingInstance.getStartLoc();
    @*/
  public /*@ pure @*/ int getStartLoc() {
    if (enclosingInstance == null) return loc;
    else return enclosingInstance.getStartLoc();
  }

  public /*@ pure @*/ int getEndLoc() {
    if (decl == null) {
      if (args.size()==0) return type.getEndLoc();
      return args.elementAt(args.size()-1).getEndLoc();
    } else return decl.getEndLoc();
  }

  //# NoMaker
  //@ requires (enclosingInstance == null) == (locDot==Location.NULL);
  //
  //@ requires loc != javafe.util.Location.NULL;
  //@ requires locOpenParen != javafe.util.Location.NULL;
  public static /*@non_null*/ NewInstanceExpr make(Expr enclosingInstance, 
                                     int locDot, 
                                     /*@ non_null @*/ TypeName type, 
                                     /*@ non_null @*/ ExprVec args, 
                                     ClassDecl anonDecl, 
                                     int loc, 
                                     int locOpenParen) {
     NewInstanceExpr result = new NewInstanceExpr(
                                  enclosingInstance,
                                  locDot,
                                  type,
                                  args,
                                  anonDecl,
                                  loc,
                                  locOpenParen);
     return result;
  }
}

public class NewArrayExpr extends Expr
{
  /**
   * The type of the elements being given zero-default values, or (if
   * an array initializer is present), the type of the array
   * initializer elements.
   *
   * <p> E.g., new int[4][3][] yields a type of int[] and new int[][][]{a, b}
   * yields a type of int[][].
   */
  //# Type type Syntax

  /**
   * The array initializer, if any.  If it is present then dims will
   * contain exactly 1 element, the inferred size of the array
   * initializer.
   *
   * <p> E.g., new int[][]{7, 5} will generate a dims of {INTLIT(2)}.
   */
  //@ invariant init != null ==> dims.count==1;
  //# ArrayInit init NullOK

  /**
   * If init is null, then holds Expr's between []'s in order.  If init
   * is not null, then holds the inferred array size.  (cf. init).
   *
   * E.g., new int[x+y][z][] will generate a dims of {<x+y>, <z>}.
   */
  //@ invariant dims.count >= 1;
  //# Expr* dims

  //# int loc NotNullLoc

  /**
   * The locations of the open brackets for each Expr (possibly
   * inferred if init != null) in dims.
   *
   * <p> The open bracket in front of dims[i] is located at
   * locOpenBrackets[i].
   *
   * @note locOpenBrackets may contain junk after the first
   * dims.size() entries.
   */
  /*@ invariant locOpenBrackets.length >= dims.count
    @	&& ((\forall int i; (0 <= i && i<dims.count) ==> 
			locOpenBrackets[i] != Location.NULL)); */
  //# int[] locOpenBrackets NoCheck

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }

  public /*@ pure @*/ int getEndLoc() { 
    if (init == null) {
      if (dims.size()<1)
        Assert.fail("Invariant failure: NewArrayExpr with init & dims>0");
      return dims.elementAt(dims.size()-1).getEndLoc();
    } else return init.locCloseBrace;
  }

  //# PostCheckCall
  private void postCheck() {
    Assert.notFalse(dims.size() >= 1);
    if (init != null)
        Assert.notFalse(dims.size()==1);
  }

  //# NoMaker
  //@ requires init != null ==> dims.count==1;
  //@ requires dims.count >= 1;
  //@ requires locOpenBrackets.length >= dims.count;
  /*@ requires (\forall int i; (0 <= i && i<dims.count) ==> 
			locOpenBrackets[i] != Location.NULL); */
  //
  //@ requires type.syntax;
  //@ requires loc != javafe.util.Location.NULL;
  public static /*@non_null*/ NewArrayExpr make(/*@ non_null @*/ Type type, 
                                  /*@ non_null @*/ ExprVec dims, 
                                  ArrayInit init, 
                                  int loc, 
                                  /*@ non_null @*/ int[] locOpenBrackets) {
     NewArrayExpr result = new NewArrayExpr(
                               type,
                               init,
                               dims,
                               loc,
                               locOpenBrackets);
     return result;
  }
}

public class CondExpr extends Expr
{
  //# Expr test
  //# Expr thn
  //# Expr els
  //# int locQuestion NotNullLoc
  //# int locColon NotNullLoc

  //@ public represents startLoc <- test.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return test.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == Math.max(thn.getEndLoc(), els.getEndLoc());
  public /*@ pure @*/ int getEndLoc() { 
    int thenL = thn.getEndLoc();
    int elseL = els.getEndLoc();
    return Math.max(thenL, elseL);
  }
}

public class InstanceOfExpr extends Expr
{
  //# Expr expr
  //# Type type Syntax
  //# int loc NotNullLoc

  //@ public represents startLoc <- expr.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return expr.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == type.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return type.getEndLoc(); }
}

public class CastExpr extends Expr
{
  //# Expr expr
  //# Type type Syntax
  //# int locOpenParen NotNullLoc
  //# int locCloseParen NotNullLoc

  //@ public represents startLoc <- locOpenParen;
  public /*@ pure @*/ int getStartLoc() { return locOpenParen; }
  //@ also public normal_behavior
  //@ ensures \result == expr.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return expr.getEndLoc(); }
}

/**
 * Represents various kinds of binary expressions (eg +,-,|,%=, etc).
 * The tag is one of the binary operator tags or assignment operator
 * tags defined in OperatorTags.
 */

public class BinaryExpr extends Expr
{
  /*@ invariant (op == TagConstants.OR || op == TagConstants.AND
       || op == TagConstants.BITOR || op == TagConstants.BITXOR
       || op == TagConstants.BITAND
       || op == TagConstants.NE || op == TagConstants.EQ
       || op == TagConstants.GE || op == TagConstants.GT
       || op == TagConstants.LE || op == TagConstants.LT
       || op == TagConstants.LSHIFT || op == TagConstants.RSHIFT
       || op == TagConstants.URSHIFT || op == TagConstants.ADD
       || op == TagConstants.SUB || op == TagConstants.DIV
       || op == TagConstants.MOD || op == TagConstants.STAR
       || op == TagConstants.ASSIGN || op == TagConstants.ASGMUL
       || op == TagConstants.ASGDIV || op == TagConstants.ASGREM
       || op == TagConstants.ASGADD || op == TagConstants.ASGSUB
       || op == TagConstants.ASGLSHIFT || op == TagConstants.ASGRSHIFT
       || op == TagConstants.ASGURSHIFT || op == TagConstants.ASGBITAND); */
  //# ManualTag
  //# int op

  //# Expr left
  //# Expr right
  //# int locOp NotNullLoc

  public final int getTag() { return op; }

  //# PostCheckCall
  private void postCheck() {
    boolean goodtag =
      (op == TagConstants.OR || op == TagConstants.AND
       || op == TagConstants.BITOR || op == TagConstants.BITXOR
       || op == TagConstants.BITAND
       || op == TagConstants.NE || op == TagConstants.EQ
       || op == TagConstants.GE || op == TagConstants.GT
       || op == TagConstants.LE || op == TagConstants.LT
       || op == TagConstants.LSHIFT || op == TagConstants.RSHIFT
       || op == TagConstants.URSHIFT || op == TagConstants.ADD
       || op == TagConstants.SUB || op == TagConstants.DIV
       || op == TagConstants.MOD || op == TagConstants.STAR
       || op == TagConstants.ASSIGN || op == TagConstants.ASGMUL
       || op == TagConstants.ASGDIV || op == TagConstants.ASGREM
       || op == TagConstants.ASGADD || op == TagConstants.ASGSUB
       || op == TagConstants.ASGLSHIFT || op == TagConstants.ASGRSHIFT
       || op == TagConstants.ASGURSHIFT || op == TagConstants.ASGBITAND
       || op == TagConstants.ASGBITOR || op == TagConstants.ASGBITXOR);
    Assert.notFalse(goodtag);
  }
  //@ public represents startLoc <- left.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return left.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == right.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return right.getEndLoc(); }

  //# NoMaker
  /*@ requires (op == TagConstants.OR || op == TagConstants.AND
       || op == TagConstants.BITOR || op == TagConstants.BITXOR
       || op == TagConstants.BITAND
       || op == TagConstants.NE || op == TagConstants.EQ
       || op == TagConstants.GE || op == TagConstants.GT
       || op == TagConstants.LE || op == TagConstants.LT
       || op == TagConstants.LSHIFT || op == TagConstants.RSHIFT
       || op == TagConstants.URSHIFT || op == TagConstants.ADD
       || op == TagConstants.SUB || op == TagConstants.DIV
       || op == TagConstants.MOD || op == TagConstants.STAR
       || op == TagConstants.ASSIGN || op == TagConstants.ASGMUL
       || op == TagConstants.ASGDIV || op == TagConstants.ASGREM
       || op == TagConstants.ASGADD || op == TagConstants.ASGSUB
       || op == TagConstants.ASGLSHIFT || op == TagConstants.ASGRSHIFT
       || op == TagConstants.ASGURSHIFT || op == TagConstants.ASGBITAND); */
  //
  //@ requires locOp != javafe.util.Location.NULL;
  public static /*@non_null*/ BinaryExpr make(int op, 
                                /*@ non_null @*/ Expr left,
			        /*@ non_null @*/ Expr right, 
                                int locOp) {
     BinaryExpr result = new BinaryExpr(
                             op,
                             left,
                             right,
                             locOp);
     return result;
  }
}

/**
 * Represents various kinds of unary expressions.
 * The tag is one of the constants:
 *      ADD SUB NOT BITNOT INC DEC POSTFIXINC POSTFIXDEC
 * defined in OperatorTags.
 */

public class UnaryExpr extends Expr
{
  /*@ invariant (op == TagConstants.UNARYADD || op == TagConstants.UNARYSUB
       || op == TagConstants.NOT || op == TagConstants.BITNOT
       || op == TagConstants.INC || op == TagConstants.DEC
       || op == TagConstants.POSTFIXINC || op == TagConstants.POSTFIXDEC); */
  //# ManualTag
  //# int op

  //# Expr expr
  //# int locOp NotNullLoc

  public final int getTag() { return op; }

  //# PostCheckCall
  private void postCheck() {
    boolean goodtag =
      (op == TagConstants.UNARYADD || op == TagConstants.UNARYSUB
       || op == TagConstants.NOT || op == TagConstants.BITNOT
       || op == TagConstants.INC || op == TagConstants.DEC
       || op == TagConstants.POSTFIXINC || op == TagConstants.POSTFIXDEC);
    Assert.notFalse(goodtag);
  }
  //@ public represents startLoc <- Math.min(expr.getStartLoc(), locOp);
  public /*@ pure @*/ int getStartLoc() { 
    int le = expr.getStartLoc(); 
    return le < locOp ? le : locOp;
  }

  //@ also public normal_behavior
  //@ ensures \result == Math.max(expr.getStartLoc(), locOp);
  public /*@ pure @*/ int getEndLoc() { 
    int le = expr.getEndLoc(); 
    return le < locOp ? locOp : le;
  }

  //# NoMaker
  /*@ requires (op == TagConstants.UNARYADD || op == TagConstants.UNARYSUB
       || op == TagConstants.NOT || op == TagConstants.BITNOT
       || op == TagConstants.INC || op == TagConstants.DEC
       || op == TagConstants.POSTFIXINC || op == TagConstants.POSTFIXDEC); */
  //
  //@ requires locOp != javafe.util.Location.NULL;
  public static /*@non_null*/ UnaryExpr make(int op, /*@ non_null @*/ Expr expr, int locOp) {
     UnaryExpr result = new UnaryExpr(op, expr, locOp);
     return result;
  }
}

public class ParenExpr extends Expr
{
  //# Expr expr
  //# int locOpenParen NotNullLoc
  //# int locCloseParen NotNullLoc

  //@ public represents startLoc <- locOpenParen;
  public /*@ pure @*/ int getStartLoc() { return locOpenParen; }
  //@ also public normal_behavior
  //@ ensures \result == locCloseParen;
  public /*@ pure @*/ int getEndLoc() { return locCloseParen; }
}

/**
 * Represents a Name that occurs in an Expression position. 
 * These are name-resolved to VariableAccess,
 * ExprFieldAccess or TypeFieldAccess.
 */

public class AmbiguousVariableAccess extends Expr
{
  //# Name name

  //@ public represents startLoc <- name.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return name.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == name.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return name.getEndLoc(); }
}

/**
 * Represents a simple name that is bound to a local variable declaration.
 * Is created only by the name resolution code, not by the parser.
 */

public class VariableAccess extends Expr
{
  //# Identifier id NoCheck
  //# int loc NotNullLoc
  //# GenericVarDecl decl
  //@ invariant decl.id==id;

  //# PostCheckCall
  private void postCheck() {
      Assert.notFalse(id == decl.id);
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }

  //# NoMaker
  //@ requires decl.id == id;
  //@ requires loc != javafe.util.Location.NULL;
  public static /*@non_null*/ VariableAccess make(/*@ non_null @*/ Identifier id, int loc,
                                                  /*@ non_null @*/ GenericVarDecl decl) {
	VariableAccess result = new VariableAccess(id, loc, decl);
	return result;
  }

}

/**
 * Represents various kinds of field access expressions. 
 * The FieldDecl is filled in by the name resolution code.
 */

public class FieldAccess extends Expr
{
  //# ObjectDesignator od
  //# Identifier id NoCheck
  //# int locId NotNullLoc

  //@ invariant decl == null || decl.id==id;
  public FieldDecl decl;

  //# PostCheckCall
  private void postCheck() {
    if (decl != null) {
      Assert.notFalse(id == decl.id);
      // Any other invariants here...???
    }
  }
  //@ public represents startLoc <- (od.getStartLoc() == Location.NULL) ? locId : od.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { 
    int locOd = od.getStartLoc();
    if (locOd == Location.NULL) 
      return locId;
    else
      return locOd;
  }
  //@ also public normal_behavior
  //@ ensures \result == locId;
  public /*@ pure @*/ int getEndLoc() { return locId; }

  //# NoMaker
  //@ requires locId != javafe.util.Location.NULL;
  public static /*@non_null*/ FieldAccess make(/*@ non_null @*/ ObjectDesignator od, 
                                 /*@ non_null @*/ Identifier id, 
                                 int locId) {
     FieldAccess result = new FieldAccess(
                              od,
                              id,
                              locId);
     result.decl = null; // Easier than puting an ensures on constructor
     return result;
  }
}

/**
 * Represents a Name occuring before an argument list.
 * Is created by the parser, and then resolved to either an
 * InstanceMethodAccess or ClassMethodAccess by the name resolution code.
 *
 * <p> Thus for the method call "x.y()", the "x.y" part is initially 
 * represented as a MethodName, 
 * and then resolved to a InstanceMethodAccess if "x" is a variable, 
 * or resolved to a ClassMethodAccess if "x" is a type name.
 */

public class AmbiguousMethodInvocation extends Expr
{
  //# Name name
  //# TypeModifierPragma* tmodifiers  NullOK
  //# int locOpenParen NotNullLoc
  //# Expr* args

  //@ public represents startLoc <- name.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return name.getStartLoc(); }

  /*@ also public normal_behavior
    @ ensures \result == (args.size() < 1 ?
    @		name.getEndLoc() : args.elementAt(args.size()-1).getEndLoc());
    @*/
  public /*@ pure @*/ int getEndLoc() { 
    if (args.size()<1)
      return name.getEndLoc();

    return args.elementAt(args.size()-1).getEndLoc();
  }
}

/**
 * Represents a MethodInvocation. 
 */

public class MethodInvocation extends Expr
{
  //# ObjectDesignator od
  //# Identifier id NoCheck
  //# TypeModifierPragma* tmodifiers  NullOK
  //# int locId NotNullLoc
  //# int locOpenParen NotNullLoc
  //# Expr* args

  //@ invariant decl == null || decl.id==id;
  public MethodDecl decl;

  //# PostCheckCall
  private void postCheck() {
    if (decl != null) {
      Assert.notFalse(id == decl.id);
      // Any other invariants here...???
    }
  }
  //@ public represents startLoc <- (od.getStartLoc() == Location.NULL) ? locId : od.getStartLoc();
  public /*@ pure @*/ int getStartLoc() {
    int loc = od.getStartLoc();
    if (loc != Location.NULL)
      return loc;
    else
      return locId;
  }
  public /*@ pure @*/ int getEndLoc() { 
    if (args.size()<1)
      return locId;

    return args.elementAt(args.size()-1).getEndLoc();
  }

  //# NoMaker
  //@ requires locId != javafe.util.Location.NULL;
  //@ requires locOpenParen != javafe.util.Location.NULL;
  public static /*@non_null*/ MethodInvocation make(/*@ non_null @*/ ObjectDesignator od, 
                                      /*@ non_null @*/ Identifier id, 
                                      TypeModifierPragmaVec tmodifiers, 
                                      int locId, 
                                      int locOpenParen, 
                                      /*@ non_null @*/ ExprVec args) {
     MethodInvocation result = new MethodInvocation(
                                   od,
                                   id,
                                   tmodifiers,
                                   locId,
                                   locOpenParen,
                                   args);
     result.decl = null;  // Easier than puting an ensures on constructor
     return result;
  }
}

/**
 * Represents a class literal (Type . class)
 */

public class ClassLiteral extends Expr
{
  //# Type type Syntax
  //# int locDot NotNullLoc

  //@ public represents startLoc <- type.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return type.getStartLoc(); }

  //@ also public normal_behavior
  //@ ensures \result == locDot;
  public /*@ pure @*/ int getEndLoc() { return locDot; }
}

/**
 * Designates the object or type used for a field or method access.
 * Subclasses represent "expr.", "type.", or "super."
 */

public abstract class ObjectDesignator extends ASTNode
{
  //# int locDot NotNullLoc

  //@ also public normal_behavior
  //@ ensures \result == locDot;
    public /*@ pure @*/ int getEndLoc() { return locDot; }
    abstract public Type type();
}

/**
 * Represents an ObjectDesignator of the form "Expr . ".
 *
 * Is created both by the parser (eg for "(x).y"),
 * and by the name resolution code (eg for "x.y").
 */

public class ExprObjectDesignator extends ObjectDesignator
{
  //# Expr expr
  //@ public represents startLoc <- expr.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return expr.getStartLoc(); }
  public Type type;
  public Type type() { return type; }
}

/**
 * Represents a ObjectDesignator of the form "TypeName . " 
 *
 * <p> Is created from AmbiguousVariableAccess/AmbiguousMethodInvocation
 * by the name resolution code.  
 * The <code>type</code> must be an instance of either <code>TypeName</code>
 * or <code>TypeSig</code>  (found in <code>javafe.tc</code>).
 * If <code>type</code> is a <code>TypeName</code>, then an explicit
 * type name was given in the program text; if <code>type</code> 
 * is a <code>TypeSig</code>, then the type was inferred.
 */

public class TypeObjectDesignator extends ObjectDesignator
{
  //@ invariant type instanceof TypeName || type instanceof javafe.tc.TypeSig;
  //# Type type

  //# PostCheckCall
  private void postCheck() {
    Assert.notNull(type);	
  }

  //@ public represents startLoc <- (type instanceof javafe.tc.TypeSig) ? locDot : type.getStartLoc();
    public /*@ pure @*/ int getStartLoc() {
        if (!(type instanceof javafe.tc.TypeSig))
	    return type.getStartLoc();

	return locDot;
    }

  public /*@ pure @*/ Type type() { return type; }

  //# NoMaker
  //* Manual maker to ensure invariant on type satisfied
  //@ requires type instanceof TypeName || type instanceof javafe.tc.TypeSig;
  //
  //@ requires locDot != javafe.util.Location.NULL;
  public static /*@non_null*/ TypeObjectDesignator make(int locDot, 
                                          /*@ non_null @*/ Type type) {
     TypeObjectDesignator result = new TypeObjectDesignator(locDot, type);
     return result;
  }
}

/**
 * Represents a ObjectDesignator of the form "super . ".
 * Is created only by the parser, not by name resolution.
 */

public class SuperObjectDesignator extends ObjectDesignator
{
  //# int locSuper NotNullLoc

  //@ public represents startLoc <- locSuper;
  public /*@ pure @*/ int getStartLoc() { return locSuper; }
  public Type type;
  public Type type() { return type; }
}

/* ---------------------------------------------------------------------- */

/**
 * Represents a Type syntactic unit. <p>
 *
 * WARNING: unlike other AST nodes, Type and it's subtypes may
 * not have associated locations!  Locations exist only if syntax is
 * true.
 */

public abstract class Type extends ASTNode
{
    /**
     * Does this AST Node have associated locations?  True if yes.
     */
    //@ public model boolean syntax;
    //@ public represents syntax <- !isInternal;

    //# TypeModifierPragma* tmodifiers NullOK
    protected Type() {}

}


/** 
 * Used to indicate the type of an illegal operation, so that error messages
 * do not unnecessarily propagate; should only be used if the error has
 * already been reported.
 */
public class ErrorType extends Type
{
  //@ invariant_redundantly isInternal;

  //@ public represents startLoc <- Location.NULL;
  public /*@ pure @*/ int getStartLoc() { return Location.NULL; }

  protected ErrorType() {}

  public static /*@ non_null */ ErrorType make() {
    ErrorType result = new ErrorType();
    return result;
  }
}

/**
 * Represents a PrimitiveType syntactic unit. 
 * Subtypes determing the range of a valid tags
 *
 * @warning This AST node has associated locations only if syntax is
 * true.
 *
 * for backwards compatibility, the PrimitiveType makers make calls to the JavafePrimitiveType
 */
public abstract class PrimitiveType extends Type
{

    //@ invariant isValidTag();
    public abstract /*@pure*/ boolean isValidTag();
  //# ManualTag
  //# int tag

  //# int loc

    //@ also ensures \result == this.tag;
    public final /*@pure*/ int getTag() { return this.tag; }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
  protected PrimitiveType() {throw new RuntimeException("this sould never be called");}
 
}

/**
 * Represents a Java Front End PrimitiveType syntactic unit. 
 * The tag should be one of the *TYPE constants defined in TagConstants
 * (e.g., INTTYPE).
 *
 * @warning This AST node has associated locations only if syntax is
 * true.
 */

public class JavafePrimitiveType extends PrimitiveType
{
  //# ManualTag
    /*@ public normal_behavior
      @   ensures  \result == (tag == TagConstants.BOOLEANTYPE || tag == TagConstants.INTTYPE
      @  || tag == TagConstants.LONGTYPE || tag == TagConstants.CHARTYPE
      @  || tag == TagConstants.FLOATTYPE || tag == TagConstants.DOUBLETYPE
      @  || tag == TagConstants.VOIDTYPE || tag == TagConstants.NULLTYPE
      @  || tag == TagConstants.BYTETYPE || tag == TagConstants.SHORTTYPE);
      @*/
    public static /*@pure*/ boolean isValidTag(int tag) {
	return (tag == TagConstants.BOOLEANTYPE || tag == TagConstants.INTTYPE
		|| tag == TagConstants.LONGTYPE || tag == TagConstants.CHARTYPE
		|| tag == TagConstants.FLOATTYPE || tag == TagConstants.DOUBLETYPE
		|| tag == TagConstants.VOIDTYPE || tag == TagConstants.NULLTYPE
		|| tag == TagConstants.BYTETYPE || tag == TagConstants.SHORTTYPE);
    }

    /*@ also
      @ public normal_behavior
      @   ensures  \result == JavafePrimitiveType.isValidTag(tag);
      @*/
    public /*@pure*/ boolean isValidTag() {
	return JavafePrimitiveType.isValidTag(tag);
    }
  //# NoMaker
  /**
   * Normal maker that produces syntax, but requires a non-NULL location.
   */
    /*@ public normal_behavior
      @   requires  JavafePrimitiveType.isValidTag(tag);
      @   requires loc != javafe.util.Location.NULL;
      @   ensures \result.syntax;
      @*/
  public static /*@ non_null */ JavafePrimitiveType make(TypeModifierPragmaVec tmodifiers,
				   int tag, int loc) {
     JavafePrimitiveType result = new JavafePrimitiveType(
                                tmodifiers,
                                tag,
                                loc);
     return result;
  }

  /**
   * Special maker for producing non-syntax, which does not require a
   * location.
   */
    /*@ public normal_behavior
      @   requires  JavafePrimitiveType.isValidTag(tag);
      @   ensures !\result.syntax;
      @*/
  public static /*@non_null*/ JavafePrimitiveType makeNonSyntax(int tag) {
     JavafePrimitiveType result = new JavafePrimitiveType(
                                 null,
                                 tag,
                                 Location.NULL);
     return result;
  }

  // manual override for backwards compatibility
    /*@ public normal_behavior
      @   requires  JavafePrimitiveType.isValidTag(tag);
      @   requires loc != javafe.util.Location.NULL;
      @   ensures \result.syntax;
      @*/
  static public /*@non_null*/ JavafePrimitiveType make(int tag, int loc) {
    return JavafePrimitiveType.make(null, tag, loc);
  }

  //# PostCheckCall
  private void postCheck() {
    boolean goodtag = JavafePrimitiveType.isValidTag(tag);
    Assert.notFalse(goodtag); 
  }
}

public class TypeName extends Type
{
  // We always have associated locations:
  //@ invariant syntax;

  //# Name name
  //@ public represents startLoc <- name.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return name.getStartLoc(); }
  //@ also public normal_behavior
  //@ ensures \result == name.getEndLoc();
  public /*@ pure @*/ int getEndLoc() { return name.getEndLoc(); }

  // overloaded constructor for type names that
  // do not have any type modifiers
  static public /*@ non_null */ TypeName make(/*@ non_null @*/ Name name) {
    return TypeName.make(null, name);
  }

}

public class ArrayType extends Type
{
  // We have associated locations iff elemType does:
  //@ invariant elemType.syntax == syntax;

  //# Type elemType
  //# int locOpenBracket NotNullLoc

  //@ public represents startLoc <- elemType.getStartLoc();
  public /*@ pure @*/ int getStartLoc() { return elemType.getStartLoc(); }

  //@ also public normal_behavior
  //@ ensures \result == elemType.getEndLoc();
  public /*@ pure @*/ int getEndLoc() {   return Location.inc(locOpenBracket, 1);  }

  //# NoMaker
  // Generate this manually to add condition about syntax:
  //@ requires locOpenBracket != javafe.util.Location.NULL;
  //@ ensures elemType.syntax ==> \result.syntax;
  public static /*@non_null*/ ArrayType make(/*@ non_null @*/ Type elemType,
                               int locOpenBracket) {
     ArrayType result = new ArrayType(null, elemType, locOpenBracket);
     // Can't fix this since elemType is *not* injective:
     //@ assume (\forall ArrayType a; a.elemType != result);
     return result;
  }

  //@ requires locOpenBracket != javafe.util.Location.NULL;
  //@ ensures elemType.syntax ==> \result.syntax;
  public static /*@non_null*/ ArrayType make(TypeModifierPragmaVec tmodifiers,
			       /*@ non_null @*/ Type elemType,
                               int locOpenBracket) {
	ArrayType at = ArrayType.make(elemType, locOpenBracket);
	at.tmodifiers = tmodifiers;
	return at;
  }
}

/* ---------------------------------------------------------------------- */

/**
 * Treated as an immutable type. <p>
 *
 * Invariant: There is always at least one element in a Name.
 */

public abstract class Name extends ASTNode
{
    /**
     * Return our printname, which will be of one of the forms X, X.Y,
     * X.Y.Z, ...
     */
    public abstract /*@ pure non_null @*/ String printName();

    /**
     * Return a hash code for <code>this</code> such that two
     * <code>Name</code>s that are <code>equals</code> have the same
     * hash code.
     */
    public abstract int hashCode();

    /**
     * Return true if <code>other</code> is a <code>Name</code> that
     * is component-wise equal to <code>this</code>.
     */
    public abstract boolean equals(/*@ nullable @*/ Object other);

    /**
     * The number of identifiers we contain.
     */
    //@ invariant length >= 1;
    /*@ ghost public int length; */

    /** Return the number of identifiers in <code>this</code>. */
    //@ ensures \result==length;
    //@ pure
    public abstract int size();

    /**
     * Return the ith identifier of <code>this</code>.
     */
    //@ requires 0 <= i && i<length;
    //@ pure
    public abstract /*@non_null*/ Identifier identifierAt(int i);

    /**
     * Return the location of the ith identifier of <code>this</code>.
     */
    //@ requires 0 <= i && i<length;
    //@ ensures \result != Location.NULL;
    //@ pure
    public abstract int locIdAt(int i);

    /**
     * Return the location of the dot after the ith identifier of
     * <code>this</code>.
     */
    //@ requires 0 <= i && i<length-1;
    //@ ensures \result != Location.NULL;
    //@ pure
    public abstract int locDotAfter(int i);

    /**
     * Return the first <code>len</code> identifiers in
     * <code>this</code> in an array.  Requires that <code>len</code>
     * be between 0 and length of <code>this</code> inclusive.
     */
    //@ requires 0 <= len && len <= length;
    //@ ensures \nonnullelements(\result);
    //@ ensures \result.length == len;
    //@ pure
    public abstract String[] toStrings(int len);

    /**
     * Return all identifiers in <code>this</code> in an array.
     */
    //@ ensures \nonnullelements(\result);
    //@ ensures \result.length == length;
    //@ pure
    public String[] toStrings() {
	return toStrings(size());
    }

    /**
     * Make a <code>Name</code> with the given identifiers and
     * locations.  Caller must forget about the Vecs/arrays passed
     * here.
     */
    //@ requires ids.count>0;
    //@ requires ids.count==locIds.length && ids.count==locDots.length+1;
    /*@ requires (\forall int i; (0 <= i && i<locIds.length)
			==> locIds[i] != Location.NULL); */
    /*@ requires (\forall int i; (0 <= i && i<locDots.length)
			==> locDots[i] != Location.NULL); */
    //@ pure
    public static /*@non_null*/ Name make(/*@non_null*/ int[] locIds, /*@non_null*/ int[] locDots, /*@non_null*/ IdentifierVec ids) {
	int sz = ids.size();
	Assert.precondition(sz > 0 && locIds.length == sz
			&& locDots.length + 1 == sz);
	if (sz == 1)
	    return SimpleName.make(ids.elementAt(0), locIds[0]);
	else
	    return CompoundName.make(ids, locIds, locDots);
    }

    /**
     * Make a <code>Name</code> whose locations are all
     * <code>loc</code> from a <code>String</code>.
     *
     * <p> This routine parses a non-empty <code>String</code> consisting
     * of a series of dot-separated components into a <code>Name</code>.
     * 
     * precondition: <code>N.length()>0</code><p>
     */
    //@ requires N.length()>0;
    //@ requires loc != Location.NULL;
    //@ pure
    public static /*@non_null*/ Name make(/*@non_null*/ String N, int loc) {
	// Convert N to a list of its components:
	String[] components = javafe.filespace.StringUtil.parseList(N, '.');
	int sz = components.length;
	Assert.notFalse(sz>0);		//@ nowarn Pre;

	// Convert the components to Identifiers:
	IdentifierVec ids = IdentifierVec.make();
	for(int i = 0; i < sz; i++)
	    ids.addElement(Identifier.intern(components[i]));

	if (sz == 1)
	    return SimpleName.make(ids.elementAt(0), loc);

	int[] newLocIds = new int[sz];
	int[] newLocDots = new int[sz-1];
	for(int i = 0; i < sz; i++)
	    newLocIds[i] = loc;
	for(int i = 0; i < sz-1; i++)
	    newLocDots[i] = loc;

	return make(newLocIds, newLocDots, ids);
    }

    /**
     * Return a <code>Name</code> consisting of the first
     * <code>len</code> identifiers of <code>this</code>.  Requires
     * that <code>len</code> is greater than zero and less than or
     * equal to the length of <code>this</code>.
     */
    //@ requires 0<len && len <= length;
    //@ pure
    public abstract /*@non_null*/ Name prefix(int len);

    /**
     * Override getEndLoc so it refers to the actual end of us.
     */
    public /*@ pure @*/ int getEndLoc() {
	return Location.inc(getStartLoc(),
			    Math.max(0, printName().length()-1));
    }

    /**
     * Avoid allocating more than one of these.
     */
    //@ invariant \nonnullelements(emptyStringArray);
    //@ invariant emptyStringArray.length == 0;
    final protected static String[] emptyStringArray = new String[0];

}

public class SimpleName extends Name
{
  //# Identifier id NoCheck

  //# int loc NotNullLoc

  //@ invariant length == 1;

  /**
   * @return a String representation of <code>this</code> in Java's
   * syntax.
   */
  public /*@non_null*/ String printName() {
    return id.toString();
  }

    public boolean equals(/*@ nullable \readonly @*/ Object other) {
	if (other instanceof SimpleName)
	    return id == ((SimpleName)other).id;
	else
	    return false;
    }

  public /*@non_null*/ Name prefix(int len) {
    Assert.precondition(len == 1);
    return make(id, loc);
  }

  /** Return a hash code for <code>this</code> such that two
    <code>Name</code>s that are <code>equals</code> have the same hash
    code.
   */
  public int hashCode() {
    return id.hashCode();
  }

  /**
   * @return the first <code>len</code> identifiers in
   * <code>this</code> in an array.  Requires that <code>len</code> be
   * between 0 and length of <code>this</code> inclusive.
   */
  public String[] toStrings(int len) {
    Assert.precondition(len == 0 || len == 1);
    if (len == 0) return emptyStringArray;
    String[] result = { id.toString() };
    return result;
  }

  /** Return the number of identifiers in <code>this</code>. */
  public int size() { return 1; }

  /** Return the ith identifier of <code>this</code>. */
  public /*@non_null*/ Identifier identifierAt(int i) {
    if (i != 0) throw new ArrayIndexOutOfBoundsException();
    return id;
  }

  /** Return the location of the ith identifier of <code>this</code>. */
  public int locIdAt(int i) {
    if (i != 0) throw new ArrayIndexOutOfBoundsException();
    return loc;
  }

  /** Return the location of the dot after the ith identifier of
    <code>this</code>.
   */
  public int locDotAfter(int i) {
    throw new ArrayIndexOutOfBoundsException();
  }

  //@ public represents startLoc <- loc;
  public /*@ pure @*/ int getStartLoc() { return loc; }
}

public class CompoundName extends Name
{
  //@ invariant length>1;

  //@ invariant ids != null;
  //@ invariant ids.count == length;
  //# Identifier* ids NoCheck

  //@ invariant locIds.length == length;
  /*@ invariant (\forall int i; (0 <= i && i<locIds.length)
			==> locIds[i] != Location.NULL); */
  //# int[] locIds NoCheck

  //@ invariant locDots.length == length-1;
  /*@ invariant (\forall int i; (0 <= i && i<locDots.length)
			==> locDots[i] != Location.NULL); */
  //# int[] locDots NoCheck

  //# PostMakeCall
  /** Ensure there's at least two identifiers in this name. */
  private void postMake() {
    Assert.notFalse(locIds.length > 1);
  }

  //# PostCheckCall
  /** Check invariants on sizes. */
  private void postCheck() {
    Assert.notFalse(ids.size() == locIds.length);
    Assert.notFalse(locIds.length > 1);
    Assert.notFalse(locIds.length == locDots.length+1);
  }

  /**
   * @return a String representation of <code>this</code> in Java's
   * syntax.
   */
  public /*@non_null*/ String printName() {
    int sz = ids.size();
    StringBuffer result = new StringBuffer(10*sz);
    for(int i = 0; i < sz; i++) {
      if (i != 0) result.append('.');
      result.append(ids.elementAt(i).toString());
    }
    return result.toString();
  }

  /** Return true if <code>other</code> is a <code>Name</code> that
    is component-wise equal to <code>this</code>. */

  public boolean equals(/*@ nullable @*/Object other) {
    if (other instanceof CompoundName) {
      Name o = (CompoundName)other;
      int sz = this.ids.size();
      if (sz != o.size()) return false;
      for(int i = 0; i < sz; i++) {
	if (this.ids.elementAt(i) != o.identifierAt(i)) return false;
      }
      return true;
    } else
      return false;
  }

  /** Return a <code>Name</code> consisting of the first
    <code>len</code> identifiers of <code>this</code>.  Requires that
    <code>len</code> is greater than zero and less than the length of
    <code>this</code>. */

  public /*@non_null*/ Name prefix(int len) {
    Assert.precondition(len > 0 && len <= ids.size());
    if (len == 1)
      return SimpleName.make(ids.elementAt(0), locIds[0]);

    Identifier[] newIds = new Identifier[len];
    int[] newLocIds = new int[len];
    int[] newLocDots = new int[len-1];
    for(int i = 0; i < len; i++) newIds[i] = this.ids.elementAt(i);
    System.arraycopy(locIds, 0, newLocIds, 0, len);
    System.arraycopy(locDots, 0, newLocDots, 0, len-1);
    return make(IdentifierVec.make(newIds), newLocIds, newLocDots);
  }

  /** Return a hash code for <code>this</code> such that two
    <code>Name</code>s that are <code>equals</code> have the same hash
    code. */

  public int hashCode() {
    int result = 0;
    for(int i = 0, sz = ids.size(); i < sz; i++)
      result += ids.elementAt(i).hashCode();
    return result;
  }

  /**
   * @return the first <code>len</code> identifiers in
   * <code>this</code> in an array.  Requires that <code>len</code> be
   * between 0 and length of <code>this</code> inclusive.
   */
  public String[] toStrings(int len) {
    Assert.precondition(0 <= len && len <= ids.size());
    if (len == 0) return emptyStringArray;
    String[] result = new String[len];
    for(int i = 0; i < len; i++)
      result[i] = ids.elementAt(i).toString();
    return result;
  }

  /** Return the number of identifiers in <code>this</code>. */
  public int size() { return ids.size(); }

  /** Return the ith identifier of <code>this</code>. */
  public /*@non_null*/ Identifier identifierAt(int i)
	/*throws ArrayIndexOutOfBoundsException*/ {
    return ids.elementAt(i);
  }

  /** Return the location of the ith identifier of <code>this</code>. */
  public int locIdAt(int i) { return locIds[i]; }
    
  /** Return the location of the dot after the ith identifier of
    <code>this</code>. */
  public int locDotAfter(int i) { return locDots[i]; }

  //@ public represents startLoc <- locIds[0];
  public /*@ pure @*/ int getStartLoc() { return locIds[0]; }

  //# NoMaker
  //@ requires ids.count>1;
  //@ requires locIds.length == ids.count;
  /*@ requires (\forall int i; (0 <= i && i<locIds.length)
			==> locIds[i] != Location.NULL); */
  //@ requires locDots.length == ids.count-1;
  /*@ requires (\forall int i; (0 <= i && i<locDots.length)
			==> locDots[i] != Location.NULL); */
  //
  public static /*@non_null*/ CompoundName make(/*@ non_null @*/ IdentifierVec ids, 
                                  /*@ non_null @*/ int[] locIds, 
                                  /*@ non_null @*/ int[] locDots) {
     CompoundName result = new CompoundName(ids, locIds, locDots);
    
     //@ set result.length = ids.count;
     result.postMake();
     return result;
  }
}

public abstract class ModifierPragma extends ASTNode
{
  /* denotes that a pragma is redundant (e.g. "requires_redundantly") */
  //# boolean redundant

  public boolean isRedundant() { return redundant; }
  public void setRedundant(boolean v) { redundant = v; }
  // when there are various synonomus tag names, tag and getTag() will be
  // a canonical value; originalTag will be the specific value.  Thus tag
  // can be used in switch statements and originalTag for printing. 
  private int originalTag;
  public ModifierPragma setOriginalTag(int t) { originalTag = t; return this; }
  public int originalTag() {
    return (originalTag == 0) ? getTag() : originalTag;
  }
  protected ModifierPragma() {}
}

public abstract class LexicalPragma extends ASTNode
{ }

public abstract class TypeModifierPragma extends ASTNode
{ }

/* ---------------------------------------------------------------------- */


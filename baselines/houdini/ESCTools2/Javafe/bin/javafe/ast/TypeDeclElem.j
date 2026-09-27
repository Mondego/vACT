package javafe.ast;

//@ model import javafe.tc.TagConstants;

/**
 ** Represents either a ClassBodyDeclaration or an
 ** InterfaceMemberDeclaration.
 **/

public interface TypeDeclElem {

    /** Return the tag of a node. */
    public int getTag();

    public void accept(/*@ non_null */ Visitor v);

    public /*@ non_null */ Object accept(/*@ non_null */ VisitorArgResult v, /*@ nullable */ Object o);	

    /**
     ** Do we have a parent field? <p>
     **
     ** A TypeDecl "invariant" requires all of its TypeDeclElem elements
     ** to have a parent. <p>
     **
     ** Known cases without parents:<p>
     **
     **   (a) non-member type TypeDecl's
     **   (b) TypeDeclElems before they are installed in a TypeDecl
     **   (c) The length FieldDecl  (belongs to no TypeDecl)
     **/
     //@ ghost public boolean hasParent;

    /**
     ** The TypeDecl we are an element of, or null if we do not have a
     **  parent (cf. hasParent).
     **/
    //@ ensures hasParent ==> \result!=null;
    public TypeDecl getParent();

    public void setParent(/*@non_null*/ TypeDecl p);

    public int getModifiers();
    public void setModifiers(int m);
    public ModifierPragmaVec getPModifiers();
	
    public int getStartLoc();
}


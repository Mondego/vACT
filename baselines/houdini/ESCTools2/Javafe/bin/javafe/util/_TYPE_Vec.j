/**
 ** _TYPE_Vec: A Vector of _TYPE_'s. <p>
 **
 ** The interface to this class follows java.util.Vector, where
 ** appropriate. <p>
 **
 **
 ** This class was automatically created from the template
 ** javafe.util/_<tt>TYPE</tt>_Vec.j.<p>, i.e.
 ** $Id$
 **
 ** DO NOT EDIT!!<p>
 **/

/*
 * File: _TYPE_Vec.java <p>
 *
 * This file is a template for a vector of X.  It is filled in using
 * sed (e.g, see the Makefile for javafe.ast).  The following
 * replacements need to be done:<p>
 *
 * _TYPE_ should be globally replaced with the simple name of the
 *		element type<p>
 * _PKG_ should be globally replaced with the package the resulting
 *		Vector type should live in<p>
 * _COMPONENTPKG_ should be globally replaced with the package the
 *		element type belongs to<p>
 *
 * The resulting type will have name _TYPE_Vec.<p>
 * 
 * Example: to make a vector of java.lang.Integer's that lives in
 * javafe.ast, substitute Integer, javafe.ast, and java.lang
 * respectively.<p>
 *
 * _TYPE_Vec's may not contain nulls.<p>
 */


package _PKG_;


import java.util.Vector;
import javafe.util.StackVector;

import _COMPONENTPKG_._TYPE_;


public class  _TYPE_Vec {

    /***************************************************
     *                                                 *
     * Instance fields:				       *
     *                                                 *
     ***************************************************/

    private /*@ spec_public non_null*/ _TYPE_[/*#@nullable*/] elements;
    private /*@spec_public*/ int count;

    /*@ invariant 0 <= count 
      @        && count <= elements.length
      @        && (\forall int i; (0<=i & i<count) ==> elements[i]!=null);
      @ invariant \typeof(elements) == \type(javafe.ast.Expr[]);
      @ invariant elements.owner == this;
      @*/


    /***************************************************
     *                                                 *
     * Private constructors:			       *
     *                                                 *
     ***************************************************/

    //@ requires \nonnullelements(els);
    //@ ensures count == els.length;
    //@ pure
    private _TYPE_Vec(/*@non_null*/ _TYPE_[/*#@non_null*/] els) {
	this.count = els.length;
	this.elements = new _TYPE_[count];
	//@ set elements.owner = this;

	System.arraycopy(els,0, elements,0, count);
	// arraycopy's spec isn't strong enough ... so assume
	//@ assume (\forall int i; (0<=i & i<count) ==> els[i] == elements[i]);
    }

    //@ requires cnt >= 0;
    //@ ensures this.elements.length >= cnt;
    //@ pure
    private _TYPE_Vec(int cnt) {
	this.elements = new _TYPE_[(cnt == 0 ? 2 : cnt)];
	//@ set elements.owner = this;

	this.count = 0;
    }


    /***************************************************
     *                                                 *
     * Public maker methods:			       *
     *                                                 *
     ***************************************************/

    //@ ensures \fresh(\result);
    //@ pure
    public static /*@non_null*/ _TYPE_Vec make() { 
	return new _TYPE_Vec(0);
    }

    //@ requires count >= 0;
    //@ ensures \fresh(\result);
    public static /*@non_null*/ _TYPE_Vec make(int count) { 
	return new _TYPE_Vec(count);
    }

    //@ requires vec.elementType <: \type(_TYPE_);
    //@ requires !vec.containsNull;
    //@ ensures \fresh(\result);
    public static /*@non_null*/ _TYPE_Vec make(/*@non_null*/ Vector vec) {
	int sz = vec.size();
	_TYPE_Vec result = new _TYPE_Vec(sz);
	result.count = sz;
	vec.copyInto(result.elements);
	return result;
    }

    //@ requires \nonnullelements(els);
    //@ ensures \result.count == els.length;
    //@ ensures \fresh(\result);
    public static /*@non_null*/ _TYPE_Vec make(/*@non_null*/ _TYPE_[/*#@non_null*/] els) {
	return new _TYPE_Vec(els);
    }

    //@ requires s.vectorCount>1;
    //@ requires s.elementType <: \type(_TYPE_);
    //@ ensures \result.count == (\old(s.elementCount) - \old(s.currentStackBottom));
    // These are from pop() on s:
    //@ modifies s.vectorCount;
    //@ ensures s.vectorCount == \old(s.vectorCount)-1;
    //@ modifies s.elementCount, s.currentStackBottom;
    public static /*@non_null*/ _TYPE_Vec popFromStackVector(/*@non_null*/ StackVector s) {
	// Creates a new _TYPE_Vec from top stuff in StackVector
	int sz = s.size();
	_TYPE_Vec r = new _TYPE_Vec(sz);
	s.copyInto(r.elements);
	r.count = sz;
	s.pop();
	return r;
    }


    /***************************************************
     *                                                 *
     * Other methods:                                  *
     *                                                 *
     ***************************************************/

    //@ requires 0 <= index && index < count;
    //@ pure
    public final /*@non_null*/ _TYPE_ elementAt(int index)
	  /*throws ArrayIndexOutOfBoundsException*/ {
	if (index < 0 || index >= count)
	    throw new ArrayIndexOutOfBoundsException(index);

	javafe.util.Assert.notNull(elements[index]);
	return /*@(non_null)*/elements[index];
    }

    //@ requires 0<=index && index<count;
    public final void setElementAt(/*@non_null*/ _TYPE_ x, int index) 
	/*throws ArrayIndexOutOfBoundsException*/ {
	if (index < 0 || index >= count)
	    throw new ArrayIndexOutOfBoundsException(index);
	elements[index] = x;
    }

    //@ ensures \result==count;
    //@ pure
    public final int size() { return count; }

    public final /*@non_null*/String toString() {
	StringBuffer b = new StringBuffer();
	b.append("{_TYPE_Vec");
	for(int i = 0; i < count; i++) {
	    b.append(" ");
	    Object e = elements[i];
	    b.append(e == null ? "null" : e.toString());
	}
	b.append('}');
	return b.toString();
    }

  //@ pure
  public final /*@non_null*/ _TYPE_[/*#@non_null*/] toArray() {
      int ct = count;
      _TYPE_[] b = new _TYPE_[ ct ];
      for(int i = 0; i < ct; i++) {
	  b[i]=elements[i];
      }
      return b;
  }

  //@ pure
  public final /*@non_null*/ _TYPE_Vec copy() {
    _TYPE_Vec result = new _TYPE_Vec(count);
    result.count = count;
    System.arraycopy(elements,0,result.elements,0,count);
    return result;
  }

  public boolean contains(/*@non_null*/ _TYPE_ x) {
    for(int i = 0; i < count; i++) {
      if( elements[i] == x ) return true;
    }
    return false;
  }

  //@ modifies count, elements;
  //@ ensures count == \old(count) + 1;
  public final void addElement(/*@non_null*/ _TYPE_ x) {
    if( count == elements.length ) {
      _TYPE_[] newElements = new _TYPE_[ 2*(elements.length==0 ?
					      2 : elements.length) ];
      //@ set newElements.owner = this;

      System.arraycopy(elements, 0, newElements, 0, elements.length );
      elements = newElements;
    }
    elements[count++]=x;
  }

  //@ modifies count;
  public final boolean removeElement(/*@non_null*/ _TYPE_ x) {
    for( int i=0; i<count; i++ ) {
      if( elements[i] == x ) {
	  int ct=count;
	for( int j=i+1; j<ct; j++ ) 
	  elements[j-1]=elements[j];
	count--;
	return true;
      }
    }
    return false;
  }

    //@ requires 0<=i && i<count;
    //@ modifies count;
    public final void removeElementAt(int i) {
	int ct=count;
        for (int j=i+1; j<ct; j++) {
            elements[j-1]=elements[j];
	}	    
        count--;
    }

    //@ requires count > 0;
    //@ modifies count;
    //@ ensures count == \old(count) - 1;
    public final /*@non_null*/ _TYPE_ pop() {
        count--;
        _TYPE_ result = elements[count];
	elements[count] = null;
	return /*@(non_null)*/result;
    }


    //@ modifies count;
    //@ ensures count==0;
    public final void removeAllElements() {
	count = 0;
    }


  //@ requires 0 <= index && index <= this.count;
  //@ modifies count, elements;
  //@ ensures count == \old(count) + 1;
  public final void insertElementAt(/*@non_null*/ _TYPE_ obj, int index) {
    if( count == elements.length ) {
      _TYPE_[] newElements = new _TYPE_[ 2*(elements.length==0 ?
					      2 : elements.length) ];
      //@ set newElements.owner = this;
      System.arraycopy(elements, 0, newElements, 0, elements.length );
      elements = newElements;
    }
    //-@ loop_predicate i != count ==> elements[count] != null, i<= count; // FIXME - what are the semantics
    for(int i = count; i > index; i--) 
      elements[i] = elements[i-1];
    elements[index] = obj;
    count++;
  }

  //@ modifies count, elements;
  //@ ensures count== \old(count) + vec.count;
  public final void append(/*@non_null*/ _TYPE_Vec vec) {
      //-@ loop_predicate count == \old(count)+i, i <= vec.count; // FIXME - what are the semantics
    for( int i=0; i<vec.size(); i++)
      addElement( vec.elementAt(i) );
  }
}

/* Copyright 2000, 2001, Compaq Computer Corporation */

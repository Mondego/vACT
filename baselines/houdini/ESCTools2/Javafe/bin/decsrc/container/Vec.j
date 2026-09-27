#if 0
// This file is passed thru cpp with the following replacements:
//	_PKG_	Name of package in which the vector class should go
//	_TYPE_	Name of type
//	_VEC_	Name of generated vector class
//	_CHAR_	Optional: define for vectors of characters
//
// Example (to get a vector of characters):
//    cpp -P -C -D_PKG_=util -D_TYPE_=char -D_VEC_=CharVec Vec.j > ...
#endif

package _PKG_;

/** Expandable vectors of _TYPE_ (automatically generated via m4
    from "decsrc/container/Vec.j")
    <p>
    This class is thread safe, but instances of this class are not.
    That is, two different threads may safely access two different
    instances of this class concurrently, but they may not safely
    access the same instance of this class without external
    synchronization. */

public final class _VEC_
{
  private int count;
  private /*@non_null*/_TYPE_[] elements;

  // Invariants: count <= elements.length
  //		 predict > 0

  // Constructors
  public _VEC_ () { this(1); }

  public _VEC_ (int predict) {
    if (predict < 1) predict = 1;
    this.elements = new _TYPE_[predict];
  }

  public int length() { return count; }

  public boolean empty() { return count == 0; }

  public _TYPE_ elementAt(int index) {
    if (count <= index) throw new ArrayIndexOutOfBoundsException(index);
    return elements[index];
  }

  public void setElementAt(_TYPE_ x, int index) {
    if (count <= index) throw new ArrayIndexOutOfBoundsException(index);
    elements[index] = x;
  }

  public _TYPE_[] toArray() {
    _TYPE_[] result = new _TYPE_[count];
    System.arraycopy(elements, 0, result, 0, count);
    return result;
  }

  // XXX Exposes the rep.  Only valid until next mutation.
  public _TYPE_[] contents() {
    return elements;
  }

  public boolean contains(_TYPE_ x) {
    return (find(x) >= 0);
  }

  /** If <code>this</code> contains <code>x</code>, return an index
      <code>i</code> such that <code>this.elementAt(i) == x</code>.
      Else return -1. */
  public int find(_TYPE_ x) {
    for(int i = 0; i < count; i++)
      if (elements[i] == x) return i;
    return -1;
  }

  public void append(_TYPE_ x) {
    if (count == elements.length) expand();
    elements[count++] = x;
  }

  public void appendArray(/*@non_null*/_TYPE_[] list) {
    appendArray(list, 0, list.length);
  }

  public void appendArray(/*@non_null*/_TYPE_[] list, int start, int num) {
    while ((count + num) > elements.length) {
      expand();
    }
    System.arraycopy(list, start, elements, count, num);
    count += num;
  }    

  public void appendVector(/*@non_null*/_VEC_ list) {
    appendArray(list.elements, 0, list.count);
  }

  public void appendVector(/*@non_null*/_VEC_ list, int start, int n) {
    if (start < 0) throw new ArrayIndexOutOfBoundsException(start);
    if ((start + n - 1) >= list.count)
      throw new ArrayIndexOutOfBoundsException(start + n - 1);
    appendArray(list.elements, start, n);
  }

#ifdef _CHAR_
  // Operations specific to vectors of characters
  public /*@non_null*/String toString() {
    return new String(elements, 0, count);
  }

  public void appendString(/*@non_null*/String s) {
    appendString(s, 0, s.length());
  }

  public void appendString(/*@non_null*/String s, int start, int num) {
    while ((count + num) > elements.length) {
      expand();
    }
    s.getChars(start, start + num, elements, count);
    count += num;
  }
#endif

  /** Insert "x" just before the element at "index".  After this
      routine returns, "x" is at "index", and succeeding elements
      have all been shifted to one higher index. */
  public final void insertElementAt(_TYPE_ x, int index) {
    if (count < index) throw new ArrayIndexOutOfBoundsException(index);
    if (count == elements.length) expand();
    System.arraycopy(elements, index, elements, index+1, count - index);
    elements[index] = x;
    count++;
  }

  public _TYPE_ remove() {
    _TYPE_ result = elements[count-1];
    count--;
    return result;
  }

  /** Remove last "n" elements from the vector. */
  public void remove(int n) {
    if (n < 0) throw new ArrayIndexOutOfBoundsException(count);
    if (n > count) throw new ArrayIndexOutOfBoundsException(-1);
    count -= n;
  }

  /** Remove first occurrence (if any) of "x".  Returns true iff an
      occurrence was found. */
  public boolean removeElement(_TYPE_ x) {
    int index = find(x);
    if (index >= 0) {
      System.arraycopy(elements, index+1, elements, index, count - index - 1);
      count--;
      return true;
    } else {
      return false;
    }
  }

  public _TYPE_ removeElementAt(int index) {
    if (count <= index) throw new ArrayIndexOutOfBoundsException(index);
    _TYPE_ result = elements[index];
    System.arraycopy(elements, index+1, elements, index, count - index - 1);
    count--;
    return result;
  }

  public final void removeAllElements() {
    count = 0;
  }

  /** Replace all occurrences of "x" with "y" in "this".  Returns the
      number of replaced occurrences. */
  public int replaceElement(_TYPE_ x, _TYPE_ y) {
	int num = 0;
	for (int i = 0; i < count; i++) {
	    if (elements[i] == x) {
		elements[i] = y;
		num++;
	    }
	}
	return num;
  }

  /** Copies this vector.  The elements are <strong>not</strong> copied. */
  public _VEC_ copy() {
    _VEC_ v = new _VEC_ (count);
    v.count = count;
    System.arraycopy(elements, 0, v.elements, 0, count);
    return v;
  }

  private void expand() {
    _TYPE_[] newElements = new _TYPE_[2*elements.length];
    System.arraycopy(elements, 0, newElements, 0, elements.length);
    elements = newElements;
  }
}

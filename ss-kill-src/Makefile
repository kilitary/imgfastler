CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -O2
LDFLAGS ?=

TARGET = ss-kill
SRCS = main.cpp
OBJS = $(SRCS:.cpp=.o)

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)
	chmod 4755 $@

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET)
